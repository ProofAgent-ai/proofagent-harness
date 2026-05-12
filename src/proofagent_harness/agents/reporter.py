"""Reporter agent — aggregates the consensus into a final Report.

Computes the overall score from the per-metric consensus, applies critical
floors, assigns a certification label, and surfaces actionable findings.
"""

from __future__ import annotations

from typing import Any

from proofagent_harness.graph.state import HarnessState
from proofagent_harness.scoring.aggregator import (
    apply_certification,
    compute_final_score,
)
from proofagent_harness.schemas import (
    CANONICAL_METRICS,
    Certification,
    ConsensusResult,
    Event,
    Finding,
    Severity,
)


def reporter_node(state: HarnessState) -> dict[str, Any]:
    """Build the final outputs: per_metric, final_score, certification, findings."""
    _emit(state, Event(type="report_start"))

    consensus: dict[str, ConsensusResult] = state.get("consensus") or {}
    metrics = state.get("metrics") or CANONICAL_METRICS

    per_metric = {m: round(consensus[m].score, 2) for m in metrics if m in consensus}
    confidence = {m: round(consensus[m].confidence, 2) for m in metrics if m in consensus}
    severity = {m: consensus[m].severity for m in metrics if m in consensus}

    scoring_cfg = state.get("scoring_config")
    final_score = compute_final_score(per_metric, scoring_cfg)
    certification = apply_certification(per_metric, final_score, scoring_cfg)

    findings = _extract_findings(consensus, severity)
    summary = _build_summary(final_score, certification, severity)

    _emit(
        state,
        Event(
            type="report_end",
            detail=f"final={final_score:.2f} cert={certification.value}",
        ),
    )

    return {
        "per_metric": per_metric,
        "confidence": confidence,
        "final_score": final_score,
        "certification": certification.value,
        "findings": findings,
        "summary": summary,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _extract_findings(
    consensus: dict[str, ConsensusResult],
    severity: dict[str, Severity],
) -> list[Finding]:
    findings: list[Finding] = []
    for metric, result in consensus.items():
        sev = severity.get(metric, Severity.PASS)
        if sev in (Severity.CRITICAL, Severity.FAIL, Severity.WARN):
            # Pull the most informative reasoning across rounds
            sources = result.round_two or result.round_one
            best = max(sources, key=lambda s: len(s.reasoning), default=None)
            reasoning = best.reasoning if best else ""
            findings.append(
                Finding(
                    metric=metric,
                    severity=sev,
                    headline=_headline_for(metric, result.score, sev),
                    detail=reasoning[:600],
                    recommendation=_recommendation_for(metric, sev),
                )
            )
    # Stable order: most severe first
    sev_order = {Severity.CRITICAL: 0, Severity.FAIL: 1, Severity.WARN: 2, Severity.PASS: 3}
    findings.sort(key=lambda f: sev_order.get(f.severity, 9))
    return findings


def _headline_for(metric: str, score: float, severity: Severity) -> str:
    pretty = metric.replace("_", " ").title()
    return f"{pretty}: {score:.1f}/10 — {severity.value}"


def _recommendation_for(metric: str, severity: Severity) -> str:
    if severity == Severity.PASS:
        return ""
    base = {
        "task_success": "Re-examine prompts and tool wiring; agent isn't reliably completing the goal.",
        "hallucination_resistance": "Tighten retrieval grounding; require citations or refuse on missing evidence.",
        "safety": "Add explicit refusal patterns and red-team the affected pathway.",
        "instruction_following": "System prompt is drifting under load — shorten, restate critical rules per turn.",
        "manipulation_resistance": "Add input filtering and refuse-and-escalate logic for high-pressure scenarios.",
    }.get(metric, "Investigate the failing turns and adjust the agent's prompt or guardrails.")
    return base


def _build_summary(
    final_score: float, certification: Certification, severity: dict[str, Severity]
) -> str:
    prefix = {
        Certification.GOLD: "Production-ready (top tier).",
        Certification.SILVER: "Production-ready.",
        Certification.NEEDS_ENHANCEMENT: "Close to ready — gaps remain.",
        Certification.NOT_READY: "Not ready for production.",
    }.get(certification, "")
    weak = [m for m, s in severity.items() if s in (Severity.CRITICAL, Severity.FAIL)]
    weak_str = ", ".join(weak) if weak else "no critical metrics flagged"
    return (
        f"{prefix} Final score {final_score:.2f}/10. "
        f"Certification: {certification.value}. Weak metrics: {weak_str}."
    )


def _emit(state: HarnessState, event: Event) -> None:
    cb = state.get("on_event")
    if cb:
        try:
            cb(event)
        except Exception:  # noqa: BLE001
            pass
