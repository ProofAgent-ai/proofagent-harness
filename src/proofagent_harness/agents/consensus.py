"""Consensus engine — deterministic, no LLM.

Reads round-1 (and optionally round-2) juror scores, computes per-metric
median + spread + confidence, and decides whether to trigger a re-vote.
"""

from __future__ import annotations

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


# ─────────────────────────────────────────────────────────────────────────────
# Pre-revote check (after round 1)
# ─────────────────────────────────────────────────────────────────────────────


def consensus_node(state: HarnessState) -> dict[str, Any]:
    """After round 1: compute spread per metric and decide which need re-vote."""
    threshold = float(state.get("revote_threshold") or 2.0)
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


# ─────────────────────────────────────────────────────────────────────────────
# Final consensus (after round 2 or directly after round 1)
# ─────────────────────────────────────────────────────────────────────────────


def finalize_consensus_node(state: HarnessState) -> dict[str, Any]:
    """Combine round-1 and round-2 scores into final per-metric ConsensusResult."""
    metrics = state.get("metrics") or CANONICAL_METRICS
    round_one = list(state.get("round_one_scores") or [])
    round_two = list(state.get("round_two_scores") or [])

    r1 = _group(round_one)
    r2 = _group(round_two)

    consensus: dict[str, ConsensusResult] = {}
    for metric in metrics:
        # Round 2 wins if present for this metric, else fall back to round 1.
        # ONLY count jurors that actually succeeded — !evaluated jurors had
        # an LLM error and their placeholder 0.0 must NOT enter the median.
        used = r2.get(metric) or r1.get(metric, [])
        evaluated_jurors = [s for s in used if s.evaluated]
        scores = [s.score for s in evaluated_jurors]

        if not scores:
            # Every juror failed for this metric — record evaluated=False so
            # the reporter can show "N/A" instead of a fake 5.0 score.
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
        try:
            per_metric_strategy = state["scoring_config"].per_metric  # type: ignore[index]
        except Exception:  # noqa: BLE001
            pass
        score = _aggregate(scores, per_metric_strategy)
        spread = (max(scores) - min(scores)) if len(scores) > 1 else 0.0
        confidence = max(0.0, 1.0 - (spread / 10.0))
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
        )

    return {"consensus": consensus}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _group(scores: list[JurorScore]) -> dict[str, list[JurorScore]]:
    out: dict[str, list[JurorScore]] = {}
    for s in scores:
        out.setdefault(s.metric, []).append(s)
    return out


def _aggregate(scores: list[float], strategy: str) -> float:
    if strategy == "mean":
        return round(sum(scores) / len(scores), 2)
    if strategy == "min":
        return round(min(scores), 2)
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
        try:
            cb(event)
        except Exception:  # noqa: BLE001
            pass


# Optional: spread variance reporting (used by the Reporter agent)
def spread_variance(scores: list[JurorScore]) -> float:
    if len(scores) < 2:
        return 0.0
    return round(pstdev([s.score for s in scores]), 3)
