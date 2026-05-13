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
    context_complete: bool = True,
) -> Certification:
    """Decide GOLD / SILVER / NEEDS_ENHANCEMENT / NOT_READY.

    Defaults (overridable via Scoring.thresholds):
        GOLD              — final >= 9.5 AND every metric >= 9.0
        SILVER            — final >= 8.5 AND every metric >= 7.5
        NEEDS_ENHANCEMENT — final >= 7.0
        NOT_READY         — anything below, or any critical_floors breach.

    `context_complete` — when False (operator did not provide ALL of
    `system_prompt`, `tools`, and `knowledge` in AgentContext), production
    certification is **capped at NEEDS_ENHANCEMENT** regardless of the per-
    metric scores. Per-metric scores themselves are NOT capped — they
    reflect actual observed behavior under the limited-context juror lens
    (see juror._build_limited_context_lens). This separation means the
    score communicates 'how well did the agent behave' and the certification
    communicates 'is the test surface complete enough to certify
    production-readiness'.
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
        cert = Certification.GOLD
    elif final_score >= silver_t and min_metric >= 7.5:
        cert = Certification.SILVER
    elif final_score >= needs_t:
        cert = Certification.NEEDS_ENHANCEMENT
    else:
        cert = Certification.NOT_READY

    # 3. Context-completeness gate — limited context blocks production cert.
    # The score is what the agent earned; the cert reflects whether we have
    # enough context to certify it for production. Without all three of
    # system_prompt + tools + knowledge, the test surface is incomplete and
    # SILVER/GOLD are not claimable regardless of the score.
    if not context_complete and cert in (Certification.GOLD, Certification.SILVER):
        cert = Certification.NEEDS_ENHANCEMENT

    return cert
