"""Artifact-mode runner — single-shot jury evaluation orchestrator.

Reuses 100% of the existing jury / consensus / reporter pipeline by:
  1. Converting the artifact (any supported format) to text via converters.
  2. Loading the knowledge corpus from disk via loader.
  3. Building a SYNTHETIC 1-turn `Turn` where:
       question = a compact summary of role + business_case + tools_used
       answer   = the artifact text
  4. Building a HarnessState that has `transcript=[synthetic_turn]` already
     populated, `plan=None`, no agent_callable, and `mode="artifact"`.
  5. Running a SLIM LangGraph (jury_round_one → consensus → optional revote
     → finalize → reporter) — skipping planner + conductor entirely.
  6. Returning a Report with `mode="artifact"` so the dashboard and
     downstream tools can branch on it.

Live Reporting events fire from the slim graph as usual — the dashboard
sees `artifact_loaded` + `corpus_loaded` events up front, then the standard
`jury_round_start` / `juror_scored` / `consensus_check` / `report_*` stream.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from proofagent_harness.artifact.converters import ArtifactConversionError
from proofagent_harness.artifact.loader import load_corpus
from proofagent_harness.graph.state import HarnessState
from proofagent_harness.schemas import (
    AgentArtifact,
    AgentContext,
    Event,
    KnowledgeCorpus,
    Turn,
)


def build_synthetic_turn(
    *,
    artifact: AgentArtifact,
    role: str,
    business_case: str,
    tools_used: list[str] | None = None,
) -> Turn:
    """Build the synthetic 1-turn `Turn` that the jury will score.

    The `question` is a compact framing the juror uses to evaluate
    `task_success` and `instruction_following` — it tells the juror
    what the artifact was SUPPOSED to deliver. The `answer` is the
    artifact text itself.
    """
    tools_str = (
        f"\n  Tools available to the producing agent: {', '.join(tools_used)}"
        if tools_used else ""
    )
    framing = (
        f"[ARTIFACT REVIEW]\n"
        f"  Agent role: {role}\n"
        f"  Business case: {business_case}{tools_str}\n"
        f"\nThe following artifact was generated to satisfy the business case "
        f"above. Score it against your assigned metric using the knowledge "
        f"corpus in your system context as ground truth."
    )

    trap_label = (artifact.type or "artifact").lower().replace(" ", "_")
    return Turn(
        turn_index=0,
        question=framing,
        answer=artifact.generated_artifact,
        tools_called=[],
        retrievals=[],
        memory_snapshot=artifact.metadata or {},
        reasoning=None,
        trap_name=trap_label,         # surfaces in dashboard as the "turn label"
        defects=[],
    )


def derive_business_case(artifact_text: str, fallback: str = "") -> str:
    """If the user didn't supply a business_case, extract one from the
    artifact itself (first ## section's body, capped at 600 chars).

    Most artifacts open with an Executive Summary / Overview / Introduction
    that IS the business case. Auto-extracting saves the user a redundant
    re-type and reduces error from miscopying. Returns `fallback` when
    the artifact has no parseable opening section.
    """
    if not artifact_text:
        return fallback
    # Find the first `##` or `# 1.` section.
    import re as _re
    m = _re.search(r"\n##\s+[^\n]+\n([\s\S]+?)(?=\n##\s|\Z)", "\n" + artifact_text)
    if not m:
        # Try first paragraph (first 2 sentences) as last-resort fallback.
        first_chunk = artifact_text.strip().split("\n\n", 1)[0]
        return (first_chunk[:600] + "…") if len(first_chunk) > 600 else first_chunk
    body = m.group(1).strip()
    return (body[:600] + "…") if len(body) > 600 else body


async def run_artifact_eval(
    *,
    artifact: AgentArtifact,
    knowledge: KnowledgeCorpus | None,
    role: str,
    business_case: str,
    tools_used: list[str] | None,
    memory: Any | None,
    state_seed: dict[str, Any],
    on_event: Callable[[Event], None] | None = None,
    agent_trace_text: str = "",
    context: "AgentContext | None" = None,
) -> dict[str, Any]:
    """Run the artifact-mode evaluation end-to-end.

    `state_seed` is the partially-built HarnessState the Harness has already
    populated with: llm, personas, skills, scoring_config, context_budget_chars,
    consensus_strategy, debate_rounds, revote_threshold, on_event,
    cost_usd=0, tokens_used=0, metrics. The runner fills in `transcript`,
    `knowledge_text`, `context`, and `mode`, then invokes the slim graph.

    Returns the final HarnessState dict — caller (the Harness) is responsible
    for converting state → Report via the existing `_state_to_report` method.

    Emits events to `on_event`:
      * artifact_loaded — once, up-front
      * corpus_loaded   — once, after the corpus walker finishes
      * (then the standard jury_* / consensus_* / report_* events)
    """
    # 1. Surface artifact metadata for the dashboard / Live Reporting.
    fmt = "text"
    if artifact.source_path:
        suffix = artifact.source_path.rsplit(".", 1)[-1].lower() if "." in artifact.source_path else ""
        fmt = suffix or "text"
    _safe_emit(on_event, Event(
        type="artifact_loaded",
        detail=f"artifact loaded: {len(artifact.generated_artifact):,} chars",
        payload={
            "type": artifact.type or "",
            "chars": len(artifact.generated_artifact),
            "source_path": artifact.source_path or "",
            "format": fmt,
        },
    ))

    # 2. Load the knowledge corpus (if any).
    corpus_text = ""
    if knowledge is not None:
        try:
            loaded = load_corpus(knowledge)
        except Exception as exc:
            # Loader failures are non-fatal — emit a warning and continue
            # with an empty corpus so the jury can still score what it can.
            _safe_emit(on_event, Event(
                type="error",
                detail=f"corpus load failed: {type(exc).__name__}: {exc}",
            ))
            loaded = None
        if loaded is not None:
            corpus_text = loaded.text
            _safe_emit(on_event, Event(
                type="corpus_loaded",
                detail=(
                    f"corpus loaded: {len(loaded.files)} files, "
                    f"{loaded.total_chars:,} chars"
                    + (" (truncated)" if loaded.truncated else "")
                ),
                payload={
                    "n_files": len(loaded.files),
                    "total_chars": loaded.total_chars,
                    "truncated": loaded.truncated,
                    "files": loaded.files[:50],   # cap for payload size
                    "skipped": loaded.skipped[:20],
                },
            ))

    # 3. Build the synthetic Turn.
    synthetic_turn = build_synthetic_turn(
        artifact=artifact,
        role=role,
        business_case=business_case,
        tools_used=tools_used,
    )

    # 4. Build the lightweight AgentContext the juror prompt builder reads.
    #    knowledge goes into state["knowledge_text"] separately, not here,
    #    because the juror prompt assembler reads it from there.
    # Thread the producing agent's OWN context (the system_prompt + tool
    # schemas it actually had) when the caller supplies it, so the jury can
    # evaluate instruction-following + tool-call hallucination against it.
    # A static artifact may have neither — both stay None, which is fine:
    # artifact mode does not context-cap for their absence.
    supplied_sp = getattr(context, "system_prompt", None) if context else None
    merged_tools: list[Any] = list(getattr(context, "tools", None) or []) if context else []
    for t in (tools_used or []):
        if t and not any(
            (tt.get("name") if isinstance(tt, dict) else tt) == t for tt in merged_tools
        ):
            merged_tools.append({"name": t})
    ctx = AgentContext(
        system_prompt=supplied_sp,    # the producing agent's system prompt, if supplied
        knowledge=None,               # we pass via knowledge_text instead
        tools=merged_tools or None,   # supplied tool schemas + tools_used names
        memory=memory if isinstance(memory, list) else None,
        metadata={"role": role, "business_case": business_case},
    )

    # v0.5.0: business-case auto-derivation if missing.
    effective_business_case = business_case
    if not (business_case or "").strip():
        effective_business_case = derive_business_case(
            artifact.generated_artifact, fallback="(no business case supplied)"
        )
        _safe_emit(on_event, Event(
            type="business_case_autoderived",
            detail=f"auto-derived business_case from artifact ({len(effective_business_case)} chars)",
            payload={"chars": len(effective_business_case)},
        ))

    # v0.5.0: surface agent execution evidence event when provided.
    if agent_trace_text:
        _safe_emit(on_event, Event(
            type="agent_trace_loaded",
            detail=f"agent execution trace loaded: {len(agent_trace_text):,} chars",
            payload={"chars": len(agent_trace_text)},
        ))

    # Merge tools_used into trusted_references — agents typically reference
    # their own tools by name in the artifact; pre-declaring them stops the
    # juror from flagging those names as hallucinations.
    auto_trusted = list(artifact.trusted_references or [])
    for t in (tools_used or []):
        if t and t not in auto_trusted:
            auto_trusted.append(t)

    # v0.5.0 custom rubric resolution — inline dict OR loaded from
    # custom_rubric_path. Per-artifact wins over Harness-level
    # site_overrides; both extend (default) or replace the built-in.
    artifact_rubric: dict[str, str] = dict(artifact.custom_rubric or {})
    artifact_rubric_mode: str = artifact.custom_rubric_mode or "extend"
    if artifact.custom_rubric_path:
        try:
            from proofagent_harness.artifact.rubrics import parse_rubric_file
            file_rubric, file_mode = parse_rubric_file(artifact.custom_rubric_path)
            # File rubric merges into inline rubric (inline wins on key collision).
            for metric, text in file_rubric.items():
                if metric not in artifact_rubric:
                    artifact_rubric[metric] = text
            # File mode is used IF no inline mode was explicitly set
            # (i.e. it stayed at its default 'extend').
            if artifact.custom_rubric_mode == "extend" and file_mode != "extend":
                artifact_rubric_mode = file_mode
        except FileNotFoundError as exc:
            _safe_emit(on_event, Event(
                type="error",
                detail=f"custom_rubric_path not found: {exc}",
            ))
        except Exception as exc:
            _safe_emit(on_event, Event(
                type="error",
                detail=f"custom_rubric_path parse failed: {type(exc).__name__}: {exc}",
            ))

    # Harness-level site overrides come from state_seed (set by harness.py).
    site_overrides = state_seed.get("site_custom_rubrics") or {}

    # 5. Build the HarnessState. Critical: plan=None signals to the juror
    #    user-message builder to skip the "EXPECTED BEHAVIOR (from plan)"
    #    block (which would render awkwardly with no traps).
    state: HarnessState = {
        **state_seed,
        "role": role,
        "business_case": effective_business_case,
        "goal": "",  # rolled into business_case for artifact mode
        "knowledge_text": corpus_text,
        "context": ctx,
        "transcript": [synthetic_turn],
        "current_turn": 1,             # the synthetic turn is "complete"
        "round_one_scores": [],
        "round_two_scores": [],
        "metrics_to_revote": [],
        "consensus": {},
        "per_metric": {},
        "confidence": {},
        "findings": [],
        "warnings": [],
        "on_event": on_event,
        # v0.5.0 artifact-mode-only state keys (TypedDict-tolerant).
        "mode": "artifact",                                       # type: ignore[typeddict-unknown-key]
        "artifact_type": artifact.type or "",                     # type: ignore[typeddict-unknown-key]
        "trusted_references": auto_trusted,                       # type: ignore[typeddict-unknown-key]
        "validation_assertions": list(artifact.validation_assertions or []),  # type: ignore[typeddict-unknown-key]
        "agent_execution_evidence": agent_trace_text,             # type: ignore[typeddict-unknown-key]
        "domain": str(artifact.metadata.get("domain", "")),       # type: ignore[typeddict-unknown-key]
        # v0.5.0 — open rubric system: per-artifact custom + site overrides.
        "artifact_custom_rubric": artifact_rubric,                # type: ignore[typeddict-unknown-key]
        "artifact_custom_rubric_mode": artifact_rubric_mode,      # type: ignore[typeddict-unknown-key]
        "site_custom_rubrics": site_overrides,                    # type: ignore[typeddict-unknown-key]
    }

    # 6. Build and invoke the slim graph (jury → consensus → reporter).
    #    Imported lazily so package import doesn't pull in langgraph just to
    #    use ArtifactConversionError / schemas.
    from proofagent_harness.graph.builder import build_artifact_graph
    graph = build_artifact_graph()
    final_state: dict[str, Any] = await graph.ainvoke(state)
    return final_state


def _safe_emit(cb: Callable[[Event], None] | None, ev: Event) -> None:
    """Fire an event on the callback, swallowing any exception.

    Live Reporting / progress bars are best-effort by contract — never
    crash an eval because a reporter raised.
    """
    if cb is None:
        return
    try:
        cb(ev)
    except Exception:
        pass


__all__ = ["run_artifact_eval", "build_synthetic_turn", "ArtifactConversionError"]
