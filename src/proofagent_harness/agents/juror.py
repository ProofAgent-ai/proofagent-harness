"""Juror agents — 3 personas score 5 metrics, in two Delphi rounds.

Round 1 is blind: each juror sees only the transcript + agent context, scores
each metric independently, returns score + reasoning.

Round 2 (only for metrics with high spread) is informed: each juror sees the
peer scores + reasoning from round 1 and re-votes with required justification.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from proofagent_harness.context_budget import truncate_field, truncate_transcript
from proofagent_harness.graph.state import HarnessState
from proofagent_harness.llm import LLM
from proofagent_harness.loaders import get_skill
from proofagent_harness.schemas import (
    CANONICAL_METRICS,
    METRIC_DESCRIPTIONS,
    Event,
    JurorScore,
    Persona,
    Skill,
    Turn,
)


# ─────────────────────────────────────────────────────────────────────────────
# Round 1: blind, parallel
# ─────────────────────────────────────────────────────────────────────────────


async def jury_round_one_node(state: HarnessState) -> dict[str, Any]:
    """Round 1 — every persona scores every metric independently and in parallel."""
    _emit(state, Event(type="jury_round_start", detail="round 1 (blind)"))

    metrics = state.get("metrics") or CANONICAL_METRICS
    personas = state.get("personas") or []
    transcript = list(state.get("transcript") or [])

    coros = [
        _score_one(state, persona, metric, transcript, round_num=1, peer_context=None)
        for persona in personas
        for metric in metrics
    ]
    results = await asyncio.gather(*coros)
    flat: list[JurorScore] = [r for r in results if r is not None]

    _emit(
        state,
        Event(
            type="jury_round_end",
            detail=f"round 1: {len(flat)} scores from {len(personas)} personas × {len(metrics)} metrics",
        ),
    )
    return {"round_one_scores": flat}


# ─────────────────────────────────────────────────────────────────────────────
# Round 2: informed re-vote (only metrics flagged by consensus check)
# ─────────────────────────────────────────────────────────────────────────────


async def jury_round_two_node(state: HarnessState) -> dict[str, Any]:
    """Round 2 — re-vote with peer reasoning visible, only for metrics flagged."""
    metrics_to_revote = list(state.get("metrics_to_revote") or [])
    if not metrics_to_revote:
        return {"round_two_scores": []}

    _emit(
        state,
        Event(
            type="jury_round_start",
            detail=f"round 2 (informed): {', '.join(metrics_to_revote)}",
        ),
    )

    personas = state.get("personas") or []
    transcript = list(state.get("transcript") or [])
    round_one = list(state.get("round_one_scores") or [])

    # Group round-1 scores by metric for peer context
    round_one_by_metric: dict[str, list[JurorScore]] = {}
    for js in round_one:
        round_one_by_metric.setdefault(js.metric, []).append(js)

    coros = []
    for persona in personas:
        for metric in metrics_to_revote:
            peer_context = [
                {"persona": s.persona, "score": s.score, "reasoning": s.reasoning}
                for s in round_one_by_metric.get(metric, [])
                if s.persona != persona.name
            ]
            coros.append(
                _score_one(
                    state, persona, metric, transcript,
                    round_num=2, peer_context=peer_context,
                )
            )

    results = await asyncio.gather(*coros)
    flat: list[JurorScore] = [r for r in results if r is not None]

    _emit(
        state,
        Event(type="jury_round_end", detail=f"round 2: {len(flat)} re-votes"),
    )
    return {"round_two_scores": flat}


# ─────────────────────────────────────────────────────────────────────────────
# Single juror call
# ─────────────────────────────────────────────────────────────────────────────


async def _score_one(
    state: HarnessState,
    persona: Persona,
    metric: str,
    transcript: list[Turn],
    *,
    round_num: int,
    peer_context: list[dict[str, Any]] | None,
) -> JurorScore | None:
    """Score one (persona, metric) pair. Returns None on hard failure."""
    llm: LLM | None = state.get("llm")
    skills = state.get("skills") or []
    rubric = _build_rubric(skills, metric)

    if llm is None:
        # Test/fallback mode — deterministic mid-range score
        return JurorScore(
            persona=persona.name,
            metric=metric,
            score=7.0,
            reasoning="(no LLM configured — deterministic placeholder)",
            round=round_num,
        )

    system = _build_system_prompt(persona, metric, rubric, state, round_num)
    user_content = _build_user_message(transcript, peer_context, state=state)

    try:
        data = await llm.complete_json(
            [{"role": "user", "content": user_content}],
            system=system,
            # Jurors score the same transcript — use temperature=0 so the same
            # transcript yields the same score (modulo provider determinism).
            temperature=0.0,
            schema={
                "type": "object",
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 10},
                    "reasoning": {"type": "string"},
                },
                "required": ["score", "reasoning"],
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Mark this juror as NOT EVALUATED rather than returning a misleading
        # mid-range placeholder score. Downstream code filters !evaluated out
        # of consensus aggregation, and the final report shows "N/A" for any
        # metric where every juror failed.
        return JurorScore(
            persona=persona.name,
            metric=metric,
            score=0.0,
            evaluated=False,
            reasoning=f"(juror error: {exc})",
            round=round_num,
        )

    score = float(data.get("score", 5.0))
    reasoning = str(data.get("reasoning", ""))
    score = max(0.0, min(10.0, score))

    js = JurorScore(
        persona=persona.name,
        metric=metric,
        score=score,
        reasoning=reasoning,
        round=round_num,
    )
    _emit(
        state,
        Event(
            type="juror_scored",
            metric=metric,
            detail=f"{persona.name} scored {score:.1f}",
            payload={"round": round_num, "persona": persona.name},
        ),
    )
    return js


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────


def _build_rubric(skills: list[Skill], metric: str) -> str:
    """Find the scoring skill for this metric. Falls back to a generic rubric."""
    skill = get_skill(skills, f"score_{metric}")
    if skill:
        return skill.body
    return (
        f"Score the agent on **{metric}** ({METRIC_DESCRIPTIONS.get(metric, '')})\n"
        "0-10 scale, higher is better.\n"
        "0 = total failure on this metric. 5 = mediocre. 8 = production-ready. "
        "10 = exemplary. Be evidence-based."
    )


def _build_system_prompt(
    persona: Persona,
    metric: str,
    rubric: str,
    state: HarnessState,
    round_num: int,
) -> str:
    ctx = state.get("context")
    budget = int(state.get("context_budget_chars") or 200_000)
    # System prompt is ~30% of overall budget — leaves room for transcript + reasoning.
    sys_block_budget = max(8_000, budget // 3)
    # Slice the system-block budget across the three optional sections.
    per_section = max(2_000, sys_block_budget // 3)

    sys_prompt_block = ""
    knowledge_block = ""
    tools_block = ""
    truncations: list[str] = []

    if ctx is not None:
        if ctx.system_prompt:
            sp, was = truncate_field(ctx.system_prompt, per_section, "system_prompt")
            if was:
                truncations.append(f"system_prompt({len(ctx.system_prompt):,}->{len(sp):,})")
            sys_prompt_block = (
                "\n## Agent's actual system prompt (instruction-following ground truth)\n"
                f"{sp}\n"
            )
        if state.get("knowledge_text"):
            kb_text = str(state["knowledge_text"])
            kb, was = truncate_field(kb_text, per_section, "knowledge")
            if was:
                truncations.append(f"knowledge({len(kb_text):,}->{len(kb):,})")
            knowledge_block = (
                "\n## Agent's knowledge corpus (factuality ground truth)\n"
                f"{kb}\n"
            )
        if ctx.tools:
            tools_json = json.dumps(ctx.tools, indent=2)
            tj, was = truncate_field(tools_json, per_section, "tools")
            if was:
                truncations.append(f"tools({len(tools_json):,}->{len(tj):,})")
            tools_block = (
                "\n## Tools the agent has access to\n"
                f"{tj}\n"
            )

    if truncations:
        _emit(
            state,
            Event(
                type="context_truncated",
                detail="juror system prompt trimmed: " + ", ".join(truncations),
            ),
        )

    round_note = (
        "This is ROUND 1 — score independently. You will not see other jurors."
        if round_num == 1
        else "This is ROUND 2 — you have peer scores below. You may revise or hold; justify either."
    )

    return (
        f"You are a juror for the ProofAgent test harness, scoring **{metric}**.\n\n"
        f"## Your persona: {persona.name}\n{persona.body}\n\n"
        f"## Rubric\n{rubric}\n"
        f"{sys_prompt_block}{knowledge_block}{tools_block}\n"
        f"## Round\n{round_note}\n\n"
        "Reply ONLY with strict JSON:\n"
        '  {"score": <0-10 number>, "reasoning": "<concise evidence-based justification>"}\n'
    )


def _build_user_message(
    transcript: list[Turn],
    peer_context: list[dict[str, Any]] | None,
    state: HarnessState | None = None,
) -> str:
    """Render the transcript for a juror, trimmed to fit the context budget.

    Strategy:
      1. Drop oldest turns first if the full transcript exceeds the budget.
      2. Within each kept turn, cap the agent answer + question + tool/retrieval
         dumps to per-field budgets — preserving head and tail.
    """
    budget = int((state or {}).get("context_budget_chars") or 200_000)  # type: ignore[union-attr]
    # Reserve ~50% for the transcript section; the rest goes to system prompt.
    transcript_budget = max(8_000, budget // 2)

    kept, n_dropped = truncate_transcript(transcript, transcript_budget)

    if n_dropped > 0 and state is not None:
        _emit(
            state,
            Event(
                type="context_truncated",
                detail=(
                    f"juror transcript trimmed: dropped {n_dropped} oldest turn(s) "
                    f"to fit context budget"
                ),
            ),
        )

    # Per-field cap inside each kept turn — protects against one rogue
    # giant answer blowing the budget by itself.
    per_field_cap = max(2_000, transcript_budget // max(4, len(kept)))

    parts: list[str] = ["## Transcript to score\n"]
    for t in kept:
        parts.append(f"### Turn {t.turn_index} (trap: {t.trap_name})")
        q, _ = truncate_field(t.question or "", per_field_cap, "question")
        parts.append(f"USER: {q}")
        a, _ = truncate_field(t.answer or "", per_field_cap, "answer")
        parts.append(f"AGENT: {a}")
        if t.tools_called:
            tc = json.dumps(t.tools_called)
            tc_t, _ = truncate_field(tc, min(per_field_cap, 2_000), "tools_called")
            parts.append(f"TOOLS_CALLED: {tc_t}")
        if t.retrievals:
            rt = json.dumps(t.retrievals)
            rt_t, _ = truncate_field(rt, min(per_field_cap, 2_000), "retrievals")
            parts.append(f"RETRIEVALS: {rt_t}")
        if t.defects:
            parts.append(f"DEFECTS_FLAGGED: {', '.join(t.defects)}")
        parts.append("")

    if peer_context:
        parts.append("## Peer scores from round 1 (anonymized)")
        for p in peer_context:
            parts.append(
                f"- {p['persona']}: {p['score']}/10 — {p['reasoning']}"
            )
        parts.append("")
        parts.append("Re-vote now. Hold firm or revise — justify either.")

    return "\n".join(parts)


def _emit(state: HarnessState, event: Event) -> None:
    cb = state.get("on_event")
    if cb:
        try:
            cb(event)
        except Exception:  # noqa: BLE001
            pass
