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
from proofagent_harness.schemas import Event

_TRUTHY = ("1", "true", "yes", "on")


def _emit(state: HarnessState, event: Event) -> None:
    cb = state.get("on_event")
    if cb:
        cb(event)


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


def _passes(state: Any = None) -> int:
    """How many assessment passes to run before taking a per-control majority vote.

    Precedence: an explicit ``PROOFAGENT_COMPLIANCE_PASSES`` always wins, then the
    run's calibrated pass count, then 1 (single pass — today's behavior)."""
    raw = os.environ.get("PROOFAGENT_COMPLIANCE_PASSES")
    if raw is not None:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1
    if state is not None:
        cal = state.get("calibration") if hasattr(state, "get") else None
        if cal is not None:
            # Floor of 3: two passes are the minimum to MEASURE this axis's spread,
            # and a third breaks ties in the per-control majority vote. Below that the
            # axis is scored blind, which is how a 45-point swing went unnoticed.
            return max(3, min(9, int(getattr(cal, "k_compliance", 1) or 1)))
    return 1


# Tolerance for the compliance axis's own convergence, on the 0-100 axis scale.
_C_TOLERANCE = 3.0
# Escalation is DISABLED: measured across 12 runs, 8 remained unconverged at 5 passes
# (worst 23.8 pp), so the extra calls bought nothing. Holding at the vote size means the
# residual is still measured and reported — the non-convergence stays visible instead of
# being papered over with spend. Majority-voting 28 categorical control statuses is the
# wrong aggregation; the fix is to score from counted violations, not to pay for more
# votes. Raise PROOFAGENT_COMPLIANCE_PASSES explicitly if you want more.
_C_MAX_PASSES = 0


def _spread(runs: list[dict[str, Any]]) -> float | None:
    """Widest disagreement between passes on the resulting compliance score.

    None when fewer than two passes produced a score — unmeasured, never "stable"."""
    from proofagent_harness.scoring.pai import compliance_overall

    scores: list[float] = []
    for r in runs:
        c, _fw, _gaps, evaluated = compliance_overall({"compliance": r})
        if c is not None and evaluated:
            scores.append(float(c))
    if len(scores) < 2:
        return None
    return round(max(scores) - min(scores), 2)


def _consensus_frameworks(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Fuse K independent assessment passes into one by per-control MAJORITY VOTE.

    For each (framework, control) seen in any pass, the consensus status is the mode
    over all K passes (a pass that omits the control counts as ``not_evaluated``);
    ties break toward the more conservative verdict (attention > partial > met >
    not_evaluated). This stabilizes BOTH which controls are evaluated and each verdict.
    The why/proof/fix are taken from a pass that matched the consensus status, and
    framework-level fields from a representative pass. Shape matches a single pass, so
    the caller's ``merge_assessment`` is unchanged."""
    from collections import Counter, OrderedDict

    conservative = {"attention": 0, "partial": 1, "met": 2, "not_evaluated": 3}
    fw_ctrl_order: OrderedDict[Any, OrderedDict[Any, None]] = OrderedDict()
    fw_meta: dict[Any, dict[str, Any]] = {}
    per_pass: list[dict[tuple, dict[str, Any]]] = []
    for d in runs:
        seen: dict[tuple, dict[str, Any]] = {}
        for fw in d.get("frameworks") or []:
            fid = fw.get("id")
            if fid is None:
                continue
            fw_ctrl_order.setdefault(fid, OrderedDict())
            fw_meta.setdefault(fid, fw)
            for c in fw.get("controls") or []:
                cid = c.get("id")
                if cid is None:
                    continue
                fw_ctrl_order[fid].setdefault(cid, None)
                seen[(fid, cid)] = c
        per_pass.append(seen)

    frameworks: list[dict[str, Any]] = []
    for fid, ctrls in fw_ctrl_order.items():
        controls: list[dict[str, Any]] = []
        for cid in ctrls:
            statuses = [
                str((p.get((fid, cid)) or {}).get("status") or "not_evaluated")
                for p in per_pass
            ]
            counts = Counter(statuses)
            top = max(counts.values())
            consensus = sorted(
                [s for s, n in counts.items() if n == top],
                key=lambda s: conservative.get(s, 9),
            )[0]
            rep = next(
                (p[(fid, cid)] for p in per_pass
                 if p.get((fid, cid)) and str(p[(fid, cid)].get("status") or "") == consensus),
                None,
            )
            control = dict(rep) if rep else {"id": cid}
            control["id"] = cid
            control["status"] = consensus
            controls.append(control)
        fw_out = dict(fw_meta.get(fid, {"id": fid}))
        fw_out["id"] = fid
        fw_out["controls"] = controls
        frameworks.append(fw_out)
    return {"frameworks": frameworks}


def compliance_assessor_node(state: HarnessState) -> dict[str, Any]:
    """Assess the finished run against the selected frameworks. Returns
    ``{"compliance": {...}}`` on success, or ``{}`` (no-op) otherwise."""
    if not _flag_on(state):
        return {}

    # DERIVED PATH — when the run scored from checks, compliance is a JOIN over the
    # verdicts that already produced the evaluation score, not a second LLM opinion
    # about them. It costs nothing, cannot disagree with the evaluation axis, and
    # cannot disagree with itself between passes (this node's own convergence
    # measurements: 8 of 12 runs unsettled at 5 passes, worst spread 23.8 points).
    verdicts = list(state.get("check_verdicts") or [])
    if verdicts:
        from proofagent_harness.agents.consensus import _traps_by_turn
        from proofagent_harness.compliance import assess_from_checks

        data = assess_from_checks(
            verdicts, _traps_by_turn(state), _selected(state),
            context_assessment=state.get("context_engineering"),
        )
        n_assessed = sum(
            1 for fw in data["frameworks"] for c in fw["controls"]
            if c["status"] != "not_evaluated"
        )
        _emit(state, Event(
            type="compliance_assessed",
            detail=(
                f"derived from {len(verdicts)} check verdict(s): "
                f"{n_assessed} control(s) assessed, 0 LLM calls"
            ),
            payload={"derivation": "checks", "frameworks": len(data["frameworks"])},
        ))
        return {
            "compliance": data,
            "compliance_passes_run": 0,
            # Exactly reproducible by construction, so the residual is 0.0 rather
            # than None — None means "unmeasured", which would understate this.
            "compliance_residual": 0.0,
        }

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
    passes = _passes(state)
    residual: float | None = None
    actual_passes = 1
    if passes <= 1:
        data = _run_json_llm(
            state.get("llm"), system=_SYSTEM, user=prompt,
            schema=response_schema(), state=state,
        )
    else:
        # Variance reduction with MEASUREMENT: run K passes, majority-vote per
        # control, and read the spread of the per-pass compliance scores off those
        # same passes — so calibrating this axis costs no extra calls. Escalate once
        # if K passes still disagree beyond tolerance; the residual is recorded either
        # way so a run states how well its compliance axis converged.
        def _one() -> dict[str, Any] | None:
            d = _run_json_llm(
                state.get("llm"), system=_SYSTEM, user=prompt,
                schema=response_schema(), state=state,
            )
            return d if isinstance(d, dict) and isinstance(d.get("frameworks"), list) else None

        runs: list[dict[str, Any]] = [d for d in (_one() for _ in range(passes)) if d]
        residual = _spread(runs)
        if residual is not None and residual > _C_TOLERANCE and len(runs) < _C_MAX_PASSES:
            runs += [d for d in (_one() for _ in range(_C_MAX_PASSES - len(runs))) if d]
            residual = _spread(runs)
        actual_passes = len(runs)
        data = _consensus_frameworks(runs) if runs else None
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
    return {
        "compliance": merged,
        "compliance_residual": residual,
        "compliance_passes_run": actual_passes,
    }
