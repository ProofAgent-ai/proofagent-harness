"""Scoring math — pure Python, deterministic, no LLM calls."""

from proofagent_harness.scoring.aggregator import (
    apply_certification,
    compute_final_score,
)
from proofagent_harness.scoring.pai import (
    DEFAULT_WEIGHTS,
    MIN_EVALUATED_CONTROLS,
    REQUIRED_AXES,
    Axis,
    PAIResult,
    compliance_overall,
    compute_pai,
    count_critical_events,
    grade_for,
    pai_from_report,
)

# Historical PAS names (see scoring/pas.py) — same objects, old spelling.
PASResult = PAIResult
compute_pas = compute_pai
pas_from_report = pai_from_report

__all__ = [
    "DEFAULT_WEIGHTS",
    "MIN_EVALUATED_CONTROLS",
    "REQUIRED_AXES",
    "Axis",
    "PAIResult",
    "PASResult",  # back-compat alias
    "apply_certification",
    "compliance_overall",
    "compute_final_score",
    "compute_pai",
    "compute_pas",  # back-compat alias
    "count_critical_events",
    "grade_for",
    "pai_from_report",
    "pas_from_report",  # back-compat alias
]
