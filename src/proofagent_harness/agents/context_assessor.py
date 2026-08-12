"""Context assessment, moved to the FRONT of the run so it can steer what follows.

WHY IT MOVED
It used to be computed inside the reporter, dead last, which meant the Q axis was a
read-only observation: the run could report that a prompt had no injection hardening
while having spent none of its turns probing injection. Assessing the context first lets
it do two jobs it could not do from the end:

  1. the planner allocates traps toward the families Q found weakest;
  2. the scorer weights failures in an area by how unprotected that area is.

REPRODUCIBILITY. Q's numeric sub-scores read a FIXED artifact and measured 0.0pp spread
across every validation run. Its prose findings are model-generated and do wobble. So
only the numbers are allowed downstream — see `q_weights`. Feeding the prose into juror
prompts would put that wobble into every metric, which is the opposite of what coupling
the axes is meant to achieve.
"""

from __future__ import annotations

import os
from typing import Any

from proofagent_harness.graph.state import HarnessState
from proofagent_harness.schemas import Event

_TRUTHY = ("1", "true", "yes", "on")


def _emit(state: HarnessState, event: Event) -> None:
    cb = state.get("on_event")
    if cb:
        cb(event)


def assess_enabled(state: HarnessState) -> bool:
    if bool(state.get("assess_context")):
        return True
    return os.environ.get("PROOFAGENT_ASSESS_CONTEXT", "").strip().lower() in _TRUTHY


def context_assessor_node(state: HarnessState) -> dict[str, Any]:
    """Grade the supplied context BEFORE the planner picks traps.

    No-op-safe in every direction: off by default, returns ``{}`` on no context or a
    failed call, and a missing assessment simply leaves every downstream weight at 1.0.
    """
    if not assess_enabled(state):
        return {}
    if state.get("context_engineering"):
        return {}          # already assessed (artifact mode seeds it)

    # A replayed transcript carries the grade it was produced under. Re-asking the model
    # would score the same transcript against a different context grade — measured at
    # 20 pp on one criterion, which moved hallucination_resistance 16.1 pp.
    #
    # Gated on ACTUALLY REPLAYING, not merely on a stored grade existing. Reusing it on a
    # fresh run would score new turns against an old assessment, and a cached grade left
    # over from an earlier run would silently suppress the assessment entirely.
    cal = state.get("calibration")
    replaying = bool(getattr(cal, "replaying", False)) if cal else False
    stored = dict(getattr(cal, "context_engineering", None) or {}) if replaying else {}
    if stored:
        from proofagent_harness.scoring.q_weights import describe, q_weights

        weights = q_weights(stored)
        _emit(state, Event(
            type="context_assessed",
            detail=(
                f"context {float(stored.get('score') or 0) * 10:.0f}% reused from the "
                f"stored transcript (no re-grade) — {describe(weights)}"
            ),
            payload={"score": stored.get("score"), "weights": weights,
                     "source": "stored"},
        ))
        return {"context_engineering": stored, "q_weights": weights}

    try:
        from proofagent_harness.context_engineering import assess_context_engineering

        result = assess_context_engineering(
            context=state.get("context"),
            mode=str(state.get("mode") or "multi_turn"),
            model=getattr(state.get("llm"), "model", None) or "gpt-4.1-mini",
            api_base=getattr(state.get("llm"), "api_base", None),
            has_knowledge=bool(state.get("knowledge_text")),
            # THE CORPUS ITSELF, not just a flag that one exists. Grounding was graded
            # from the prompt plus a boolean, so the assessment could neither judge what
            # the corpus actually says nor quote a passage from it — and a proof that
            # cannot cite a knowledge file cannot be traced to one.
            knowledge_source=state.get("knowledge_source"),
            governance=state.get("governance_profile"),
        )
    except Exception as exc:
        _emit(state, Event(
            type="warning",
            detail=f"context assessment unavailable ({type(exc).__name__}) — "
                   f"weights stay neutral",
        ))
        return {}

    if not result:
        return {}

    # PROVENANCE. A finding about the prompt should name the file an engineer has to
    # open, not "the supplied context" — this is the only place that knows it, since
    # the reporter downstream sees the assessment but never the AgentContext.
    _sources = (getattr(state.get("context"), "metadata", None) or {}).get("_sources") or {}
    if _sources.get("system_prompt"):
        result["source_file"] = _sources["system_prompt"]

    from proofagent_harness.scoring.q_weights import describe, q_weights

    weights = q_weights(result)
    _emit(state, Event(
        type="context_assessed",
        detail=(
            f"context {float(result.get('score') or 0) * 10:.0f}% — {describe(weights)}"
        ),
        payload={"score": result.get("score"), "weights": weights},
    ))
    return {"context_engineering": result, "q_weights": weights}
