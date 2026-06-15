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


def consensus_node(state: HarnessState) -> dict[str, Any]:
    """After round 1: compute spread per metric and decide which need re-vote."""
    threshold = float(state.get("revote_threshold") or 1.0)
    strategy = str(state.get("consensus_strategy") or "delphi")

    metrics = state.get("metrics") or CANONICAL_METRICS
    round_one = list(state.get("round_one_scores") or [])
    by_metric = _group(round_one)

    metrics_to_revote: list[str] = []
    if strategy in {"delphi", "debate"}:
        for metric in metrics:
            scores = [s.score for s in by_metric.get(metric, [])]
            if len(scores) >= 2 and (max(scores) - min(scores)) > threshold:
                metrics_to_revote.append(metric)

    _emit(
        state,
        Event(
            type="consensus_check",
            detail=(
                f"{len(metrics_to_revote)} metric(s) need re-vote"
                if metrics_to_revote
                else "all metrics converged in round 1"
            ),
            payload={"metrics_to_revote": metrics_to_revote},
        ),
    )
    return {"metrics_to_revote": metrics_to_revote}

def should_revote(state: HarnessState) -> str:
    """Conditional edge: trigger Round 2 only if there are metrics to re-vote."""
    if state.get("metrics_to_revote"):
        return "revote"
    return "skip"

def finalize_consensus_node(state: HarnessState) -> dict[str, Any]:
    """Combine round-1 and round-2 scores into final per-metric ConsensusResult."""
    metrics = state.get("metrics") or CANONICAL_METRICS
    round_one = list(state.get("round_one_scores") or [])
    round_two = list(state.get("round_two_scores") or [])

    r1 = _group(round_one)
    r2 = _group(round_two)

    consensus: dict[str, ConsensusResult] = {}
    for metric in metrics:
        used = r2.get(metric) or r1.get(metric, [])
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
            revote_triggered=metric in (state.get("metrics_to_revote") or []),
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
        return round(sum(w * x for w, x in zip(weights, s)) / sum(weights), 2)
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
