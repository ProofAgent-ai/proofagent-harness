"""Compliance-assessor node — the post-jury compliance duty.

Runs AFTER the reporter (so it reuses the jury's ENRICHED findings — the synthesized
Problem/Proof/Fix — as evidence), and ONLY when the caller opts in via
``--assess-compliance`` (``state["assess_compliance"]``) or ``PROOFAGENT_COMPLIANCE=1``.
It maps the finished evaluation to the SELECTED regulatory frameworks and returns a
per-control status + why-not-compliant / proof / fix, using the harness LLM
(token-accounted) via the reporter's ``_run_json_llm`` bridge.

No-op-safe: when the flag is off, no LLM is configured, or the call fails, it writes
nothing to ``state["compliance"]`` — governance then renders those frameworks as a
neutral "not assessed" (never a red 0, never an ASSESSED stamp).
"""

from __future__ import annotations

import os
from typing import Any

from proofagent_harness.compliance import (
    _SYSTEM,
    _resolve_selection,
    build_prompt,
    merge_assessment,
    response_schema,
)
from proofagent_harness.graph.state import HarnessState

_TRUTHY = ("1", "true", "yes", "on")


def _flag_on(state: HarnessState) -> bool:
    """Assess only when explicitly requested — the flag, or PROOFAGENT_COMPLIANCE
    set truthy. Default OFF (unlike the old env-gated always-on behavior), so a
    plain `proof run` never ships placeholder compliance."""
    if bool(state.get("assess_compliance")):
        return True
    return os.environ.get("PROOFAGENT_COMPLIANCE", "").strip().lower() in _TRUTHY


def _selected(state: HarnessState) -> list[str] | None:
    """The frameworks to assess: state selection (from --frameworks / governance),
    else PROOFAGENT_COMPLIANCE_FRAMEWORKS, else None → the default core set."""
    sel = state.get("compliance_frameworks") or None
    if not sel:
        env = os.environ.get("PROOFAGENT_COMPLIANCE_FRAMEWORKS", "").strip()
        if env:
            sel = [s.strip() for s in env.split(",") if s.strip()]
    return sel


def _evidence_text(state: HarnessState, budget: int = 8000) -> str | None:
    """Behavioral evidence for the compliance model to render CONTROL verdicts.

    Artifact mode ships a distilled trace in ``agent_execution_evidence``.
    Multi-turn mode has none — so without this the assessor sees only the
    findings + scores and a careful model marks every control ``not_evaluated``
    (it won't guess). Render the transcript (per-turn question + answer + trap)
    so the model has the ACTUAL behavior to assess against. Newest turns first,
    trimmed to ``budget`` chars."""
    art = state.get("agent_execution_evidence")
    if art:
        return str(art)
    turns = list(state.get("transcript") or [])
    if not turns:
        return None
    blocks: list[str] = []
    used = 0
    for t in reversed(turns):
        trap = f" [trap: {t.trap_name}]" if getattr(t, "trap_name", "") else ""
        q = (getattr(t, "question", "") or "").strip()
        a = (getattr(t, "answer", "") or "").strip()
        block = f"Turn {getattr(t, 'turn_index', '?')}{trap}\n  User: {q}\n  Agent: {a}"
        if used + len(block) > budget:
            break
        blocks.append(block)
        used += len(block) + 2
    if not blocks:
        return None
    return "\n\n".join(reversed(blocks))


def compliance_assessor_node(state: HarnessState) -> dict[str, Any]:
    """Assess the finished run against the selected frameworks. Returns
    ``{"compliance": {...}}`` on success, or ``{}`` (no-op) otherwise."""
    if not _flag_on(state):
        return {}
    try:
        # Reuse the reporter's sync↔async JSON bridge (token-accounted, LLM fallback).
        from proofagent_harness.agents.reporter import _run_json_llm
    except Exception:
        return {}

    active = _resolve_selection(_selected(state))
    prompt = build_prompt(
        mode=str(state.get("mode") or "multi_turn"),
        final_score=float(state.get("final_score") or 0.0),
        certification=state.get("certification") or "",
        per_metric=state.get("per_metric") or {},
        findings=state.get("findings") or [],
        active=active,
        evidence_text=_evidence_text(state),
    )
    data = _run_json_llm(
        state.get("llm"), system=_SYSTEM, user=prompt,
        schema=response_schema(), state=state,
    )
    if not data or not isinstance(data.get("frameworks"), list):
        # The user EXPLICITLY asked for compliance but the harness LLM couldn't
        # produce it (small model, JSON failure, no LLM). Don't fail silently —
        # surface a warning so "not assessed" isn't mistaken for "nothing to
        # assess". The gate/score are untouched.
        model = getattr(state.get("llm"), "model", None) or "the harness LLM"
        # Merge, don't overwrite: the reporter already populated warnings and
        # this node runs after it on a last-writer-wins channel.
        return {"warnings": [
            *(state.get("warnings") or []),
            f"Compliance assessment was requested (--assess-compliance) but "
            f"{model} did not return a usable result, so no compliance was "
            f"produced. Try a stronger --llm or a --fallback-llm.",
        ]}
    # Provenance-accurate attribution: never stamp a model name we didn't use.
    model = getattr(state.get("llm"), "model", None) or "unknown"
    merged = merge_assessment(active, data, model)

    # A structurally-valid result where the model marked EVERY control
    # `not_evaluated` carries no actual assessment — governance renders it as
    # "not assessed" (zero evidence). The cause is either a tiny model punting on
    # verdicts OR (more often, on a capable model) a THIN run that gave the model
    # little behavioral evidence to tie to these controls. The warning names both,
    # scaled to how much behavior the run actually produced — so a capable model
    # isn't wrongly blamed.
    evaluated = sum(
        1
        for fw in merged.get("frameworks", [])
        for c in fw.get("controls", [])
        if str(c.get("status") or "") in ("met", "partial", "attention")
    )
    if evaluated == 0:
        model_name = getattr(state.get("llm"), "model", None) or "the harness LLM"
        turns = len(state.get("transcript") or [])
        findings_n = len(state.get("findings") or [])
        thin_run = turns <= 4 or findings_n <= 2
        cause = (
            f"This run was short ({turns} turn(s), {findings_n} finding(s)), so "
            f"there may be too little behavioral evidence to tie to these controls — "
            f"try more --turns for a richer run."
            if thin_run else
            "With a capable model this usually means the run's behavior didn't map "
            "cleanly to these specific controls (not a model-strength issue)."
        )
        return {
            "compliance": merged,
            "warnings": [
                *(state.get("warnings") or []),
                f"Compliance was requested but {model_name} returned no control "
                f"verdicts (every control 'not_evaluated'), so coverage reads as "
                f"'not assessed'. {cause} On a very small local model, also try a "
                f"stronger --llm or --fallback-llm.",
            ],
        }
    return {"compliance": merged}
