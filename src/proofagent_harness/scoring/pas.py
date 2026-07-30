"""Historical PAS names — back-compat shim over ``scoring/pai.py``.

The score was renamed **PAS (ProofAgent Score) -> PAI (ProofAgent Index)** when the
index was published: PAI is the construct (a production-readiness index), and the
number it emits is the readiness score. Nothing about the math changed in the rename.

Import ``proofagent_harness.scoring.pai`` in new code. This module keeps the old
symbols working so existing pipelines do not break:

    compute_pas       -> compute_pai
    pas_from_report   -> pai_from_report
    PASResult         -> PAIResult

Two behaviour corrections landed with the rename (they apply through these aliases
too, because there is only one implementation):

  * the compliance axis is scored over EVALUATED controls only, so a short run that
    leaves most controls ``not_evaluated`` no longer reads as non-compliant;
  * the completeness rule is enforced, so a run missing a required axis is
    PAI-Partial and yields readiness ``indeterminate`` instead of a verdict.
"""

from __future__ import annotations

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

# Historical aliases — identical objects, old names.
PASResult = PAIResult
compute_pas = compute_pai
pas_from_report = pai_from_report

__all__ = [
    "DEFAULT_WEIGHTS",
    "MIN_EVALUATED_CONTROLS",
    "REQUIRED_AXES",
    "Axis",
    "PAIResult",
    "PASResult",
    "compliance_overall",
    "compute_pai",
    "compute_pas",
    "count_critical_events",
    "grade_for",
    "pai_from_report",
    "pas_from_report",
]
