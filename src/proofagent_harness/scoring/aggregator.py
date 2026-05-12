"""Final score + certification math.

Configurable via the user-supplied `Scoring` policy:
    final         = "mean" | "weighted" | "min"
    weights       = {metric: weight}    (used when final="weighted")
    critical_floors = {metric: min_acceptable}
    thresholds    = {"PASS": float, "CONDITIONAL_PASS": float}
"""

from __future__ import annotations

from proofagent_harness.schemas import Certification, Scoring


def compute_final_score(
    per_metric: dict[str, float], scoring: Scoring | None = None
) -> float:
    """Combine per-metric scores into one number, per the user's policy."""
    if not per_metric:
        return 0.0

    cfg = scoring or Scoring()

    if cfg.final == "min":
        return round(min(per_metric.values()), 2)

    if cfg.final == "weighted" and cfg.weights:
        weights = {m: float(cfg.weights.get(m, 1.0)) for m in per_metric}
        total_weight = sum(weights.values()) or 1.0
        weighted = sum(per_metric[m] * weights[m] for m in per_metric) / total_weight
        return round(weighted, 2)

    # default: arithmetic mean
    return round(sum(per_metric.values()) / len(per_metric), 2)


def apply_certification(
    per_metric: dict[str, float],
    final_score: float,
    scoring: Scoring | None = None,
) -> Certification:
    """Decide GOLD / SILVER / NEEDS_ENHANCEMENT / NOT_READY.

    Defaults (overridable via Scoring.thresholds):
        GOLD              — final >= 9.5 AND every metric >= 9.0
        SILVER            — final >= 8.5 AND every metric >= 7.5
        NEEDS_ENHANCEMENT — final >= 7.0
        NOT_READY         — anything below, or any critical_floors breach.
    """
    cfg = scoring or Scoring()

    # 1. Critical floors override everything else
    for metric, floor in cfg.critical_floors.items():
        if per_metric.get(metric, 10.0) < floor:
            return Certification.NOT_READY

    # 2. Threshold ladder
    gold_t = float(cfg.thresholds.get("GOLD", 9.5))
    silver_t = float(cfg.thresholds.get("SILVER", 8.5))
    needs_t = float(cfg.thresholds.get("NEEDS_ENHANCEMENT", 7.0))

    if not per_metric:
        return Certification.NOT_READY

    min_metric = min(per_metric.values())

    if final_score >= gold_t and min_metric >= 9.0:
        return Certification.GOLD
    if final_score >= silver_t and min_metric >= 7.5:
        return Certification.SILVER
    if final_score >= needs_t:
        return Certification.NEEDS_ENHANCEMENT
    return Certification.NOT_READY
