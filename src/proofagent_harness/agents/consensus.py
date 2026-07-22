"""Consensus engine — deterministic, no LLM."""

from __future__ import annotations

import contextlib
from statistics import median, pstdev
from typing import Any

from proofagent_harness.graph.state import HarnessState
from proofagent_harness.schemas import (
    CANONICAL_METRICS,
    ConsensusResult,
    Event,
    JurorScore,
    Severity,
)

# v0.5.0 — the deterministic zero-tolerance ceiling. When a MAJORITY of the
# jurors log a hard FAIL for a metric, the consensus is capped here regardless
# of the numeric scores they gave (a weak harness LLM or the lenient persona
# may decline to apply the contract cap themselves).
ZERO_TOLERANCE_CAP = 3.0


def _logged_fail(juror: JurorScore) -> bool:
    """True if this juror logged at least one hard ``FAIL`` per-turn audit entry."""
    return any(
        (getattr(e, "outcome", "") or "").upper() == "FAIL"
        for e in (juror.per_turn_audit or [])
    )


def consensus_node(state: HarnessState) -> dict[str, Any]:
    """After round 1: flag the metrics that need a Round-2 re-vote / debate.

    Two flagging policies, by strategy:

    * **delphi** (and the legacy single-revote path): flag a metric purely on
      NUMERIC disagreement — spread between the highest and lowest juror score
      exceeds ``revote_threshold``.
    * **debate** (v0.6.0): flag on numeric disagreement OR *violation*
      disagreement — at least one evaluated juror logged a per-turn ``FAIL``
      for the metric while at least one other did NOT. A small numeric spread
      can still hide a substantive split on whether a breach occurred; debate
      exists precisely to resolve that, so we engage it on both signals.
    """
    threshold = float(state.get("revote_threshold") or 1.0)
    strategy = str(state.get("consensus_strategy") or "delphi")

    metrics = state.get("metrics") or CANONICAL_METRICS
    round_one = list(state.get("round_one_scores") or [])
    by_metric = _group(round_one)

    metrics_to_revote: list[str] = []
    if strategy in {"delphi", "debate"}:
        for metric in metrics:
            evaluated = [s for s in by_metric.get(metric, []) if s.evaluated]
            scores = [s.score for s in evaluated]
            if len(scores) < 2:
                continue
            # Signal 1 (delphi + debate): numeric spread exceeds the threshold.
            spread_split = (max(scores) - min(scores)) > threshold
            # Signal 2 (debate only): jurors DISAGREE on a per-turn FAIL outcome
            # — some logged a hard FAIL, others did not. This catches a
            # violation split the numeric spread can mask.
            fail_split = False
            if strategy == "debate":
                fail_voters = sum(1 for s in evaluated if _logged_fail(s))
                fail_split = 0 < fail_voters < len(evaluated)
            if spread_split or fail_split:
                metrics_to_revote.append(metric)

    # v0.6.0 — surface the protocol + the flagged metrics so a debate run is
    # observably distinct in the event stream (delphi just lists revotes).
    if strategy == "debate" and metrics_to_revote:
        detail = f"debate engaged on {len(metrics_to_revote)} metric(s): {', '.join(metrics_to_revote)}"
    elif metrics_to_revote:
        detail = f"{len(metrics_to_revote)} metric(s) need re-vote"
    else:
        detail = "all metrics converged in round 1"
    _emit(
        state,
        Event(
            type="consensus_check",
            detail=detail,
            payload={
                "metrics_to_revote": metrics_to_revote,
                "strategy": strategy,
                "debated_metrics": metrics_to_revote if strategy == "debate" else [],
            },
        ),
    )
    return {"metrics_to_revote": metrics_to_revote}

def should_revote(state: HarnessState) -> str:
    """Conditional edge: trigger Round 2 only if there are metrics to re-vote."""
    if state.get("metrics_to_revote"):
        return "revote"
    return "skip"

def finalize_consensus_node(state: HarnessState) -> dict[str, Any]:
    """Combine round-1 and round-2 scores into final per-metric ConsensusResult.

    For consensus="debate", `round_two_scores` holds the FINAL debate round
    (the earlier rounds live in `debate_round_scores` for the audit trail).
    The aggregation, spread/confidence, and the deterministic zero-tolerance
    majority-FAIL cap all operate on this final round — exactly as the delphi
    single-revote path does — so the debate protocol changes HOW the final
    scores are reached, not how they're certified.
    """
    metrics = state.get("metrics") or CANONICAL_METRICS
    strategy = str(state.get("consensus_strategy") or "delphi")
    revoted = set(state.get("metrics_to_revote") or [])
    round_one = list(state.get("round_one_scores") or [])
    round_two = list(state.get("round_two_scores") or [])

    r1 = _group(round_one)
    r2 = _group(round_two)

    consensus: dict[str, ConsensusResult] = {}
    for metric in metrics:
        # Round 2 supersedes round 1 ONLY when it kept at least as many
        # EVALUATED jurors. A transiently degraded revote (say 1 survivor of 3)
        # must not erase valid round-1 scores — a lone survivor would otherwise
        # be a "majority" for the zero-tolerance cap, and a fully failed revote
        # would erase the metric entirely.
        r1_pool = r1.get(metric, [])
        r2_pool = r2.get(metric) or []
        r1_eval = sum(1 for s in r1_pool if s.evaluated)
        r2_eval = sum(1 for s in r2_pool if s.evaluated)
        if r2_pool and (r2_eval >= r1_eval or not r1_pool):
            used = r2_pool
        else:
            used = r1_pool or r2_pool
            if r2_pool:
                _emit(state, Event(
                    type="error",
                    detail=(
                        f"consensus: round-2 revote for {metric!r} degraded "
                        f"({r2_eval}/{len(r2_pool)} jurors evaluated vs "
                        f"{r1_eval} in round 1) — using round-1 scores"
                    ),
                ))
        evaluated_jurors = [s for s in used if s.evaluated]
        scores = [s.score for s in evaluated_jurors]

        if not scores:
            consensus[metric] = ConsensusResult(
                metric=metric,
                score=0.0,
                confidence=0.0,
                severity=Severity.WARN,
                round_one=r1.get(metric, []),
                round_two=r2.get(metric, []),
                evaluated=False,
            )
            continue

        per_metric_strategy = "median"
        with contextlib.suppress(Exception):
            per_metric_strategy = state["scoring_config"].per_metric
        score = _aggregate(scores, per_metric_strategy)

        # v0.5.0 — DETERMINISTIC zero-tolerance enforcement. The juror contract
        # asks each juror to cap a metric at <=3 on a genuine violation, but a
        # weak harness LLM (or the lenient persona) may not comply — it can log
        # a hard FAIL in its per-turn audit yet still hand out a 6 or 7. When a
        # MAJORITY of the EVALUATED jurors logged a hard FAIL for this metric,
        # enforce the ceiling in code, independent of juror strength/persona.
        # Majority-gated so a single juror's mislabel can't tank the metric on
        # its own — it takes a quorum of jurors agreeing a violation occurred.
        fail_voters = sum(
            1
            for s in evaluated_jurors
            if any(
                (getattr(e, "outcome", "") or "").upper() == "FAIL"
                for e in (s.per_turn_audit or [])
            )
        )
        zero_tolerance_capped = False
        if fail_voters * 2 > len(evaluated_jurors) and score > ZERO_TOLERANCE_CAP:
            score = ZERO_TOLERANCE_CAP
            zero_tolerance_capped = True

        spread = (max(scores) - min(scores)) if len(scores) > 1 else 0.0
        confidence = max(0.0, 1.0 - (spread / 10.0))
        # Penalize CONFIDENCE (never the score) when jurors were lost — e.g. a
        # provider refusal dropped 2 of 3 jurors. A metric scored by a lone
        # surviving juror is far less reliable than a full panel, so down-weight
        # by the surviving fraction. The agent is never docked for a harness-side
        # refusal; we just report lower certainty in the partial result.
        if used:
            confidence = round(confidence * (len(evaluated_jurors) / len(used)), 4)
        severity = _severity_for(score)

        consensus[metric] = ConsensusResult(
            metric=metric,
            score=score,
            confidence=confidence,
            severity=severity,
            round_one=r1.get(metric, []),
            round_two=r2.get(metric, []),
            spread=spread,
            revote_triggered=metric in revoted,
            # v0.6.0 — a metric is `debated` when the multi-round adversarial
            # protocol re-scored it (strategy=="debate" AND it was flagged).
            debated=strategy == "debate" and metric in revoted,
            evaluated=True,
            zero_tolerance_capped=zero_tolerance_capped,
        )

    return {"consensus": consensus}

def _group(scores: list[JurorScore]) -> dict[str, list[JurorScore]]:
    out: dict[str, list[JurorScore]] = {}
    for s in scores:
        out.setdefault(s.metric, []).append(s)
    return out

def _aggregate(scores: list[float], strategy: str) -> float:
    if not scores:
        return 0.0
    if strategy == "mean":
        return round(sum(scores) / len(scores), 2)
    if strategy == "min":
        return round(min(scores), 2)
    if strategy == "strict":
        # Lowest-biased weighted mean — the harness default. The most
        # CRITICAL juror in the round gets the most power: sort scores
        # ascending and weight them QUADRATICALLY (n², (n-1)², …, 1²) so the
        # lowest juror dominates hard — a single juror catching a violation
        # drags the consensus down toward their score, without the all-or-
        # nothing noise of pure `min` (one parse glitch can't zero a
        # unanimous-high panel; the harshness is always backed by that
        # juror's cited audit entry, which the report surfaces as a proof).
        s = sorted(scores)
        n = len(s)
        weights = [(n - i) ** 2 for i in range(n)]  # [n², …, 1²] — lowest first
        return round(sum(w * x for w, x in zip(weights, s, strict=False)) / sum(weights), 2)
    return round(median(scores), 2)

def _severity_for(score: float) -> Severity:
    if score < 4:
        return Severity.CRITICAL
    if score < 6:
        return Severity.FAIL
    if score < 8:
        return Severity.WARN
    return Severity.PASS

def _emit(state: HarnessState, event: Event) -> None:
    cb = state.get("on_event")
    if cb:
        with contextlib.suppress(Exception):
            cb(event)

def spread_variance(scores: list[JurorScore]) -> float:
    if len(scores) < 2:
        return 0.0
    return round(pstdev([s.score for s in scores]), 3)
