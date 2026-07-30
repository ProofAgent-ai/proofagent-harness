"""ProofAgent Index (PAI) — compute the 0-100 production readiness index.

PAI fuses four independently measured axes into one admissibility decision:

    E  evaluation   does the agent behave
    Q  context      is it engineered to
    C  compliance   is it lawful
    G  governance   is it controlled

Nothing here needs an API key or a live run: every part is pure, deterministic math
over axis scores. Run it as-is:

    python examples/11_pai_readiness_index.py

The CLI equivalent of each section is shown in a comment above it.
"""

from __future__ import annotations

import json

from proofagent_harness.scoring.pai import compute_pai, pai_from_report

# ── 1. Score four axes directly ────────────────────────────────────────────────
# CLI:  proof pai -E 74.3 -Q 68.6 -C 24.0 -G 66.0 --explain
#
# These are the real numbers from the finance/mid/B0 cell of the published PAI
# readiness study: an agent that PASSED a behavioral bar of 70 (E = 74.3) and would
# have shipped on a benchmark score, but whose compliance evidence says 24.
result = compute_pai(evaluation=74.3, context=68.6, compliance=24.0, governance=66.0)

print("1. Four axes, scored directly")
print(f"   PAI          {result.score}/100  ({result.grade} · {result.band})")
print(f"   verdict      {result.verdict}")
print(f"   completeness {result.completeness}")
print(f"   weakest      {result.weakest}  <- fix this layer first")
print(f"   (an arithmetic mean would have said {(74.3 + 68.6 + 24.0 + 66.0) / 4:.1f})")

# ── 2. Limited compensation, and its honest boundary ──────────────────────────
# A weak axis drags the composite down harder than an average would...
strong_but_one_gap = compute_pai(
    evaluation=100, context=100, compliance=100, governance=25,
)
# ...but the geometric mean LIMITS compensation, it does not eliminate it: three
# perfect axes still pull a 25 up to ~71. Critical deficiencies are the cap's job.
print("\n2. Limited compensation")
print(f"   (100, 100, 100, 25) -> {strong_but_one_gap.score}   "
      f"arithmetic would say {(100 + 100 + 100 + 25) / 4:.1f}")

# ── 3. The completeness rule ───────────────────────────────────────────────────
# CLI:  proof pai --report report.json --require-complete
#
# Absence of evidence is not evidence of readiness. With no compliance evidence, a
# high-scoring agent gets a diagnostic number but NO verdict.
partial = compute_pai(evaluation=90, context=90, compliance=None, governance=90)
print("\n3. Completeness rule")
print(f"   PAI          {partial.score}/100   (looks healthy)")
print(f"   verdict      {partial.verdict}")
print(f"   missing      {partial.missing_axes}")
print(f"   readiness    {partial.readiness}   <- not admissible, not rejected either")

# ── 4. The hard block: gate vs gauge ──────────────────────────────────────────
# A prohibited use case, a critical floor breach, a critical defect, or a critical
# finding caps PAI into the F band no matter how good everything else is.
blocked = compute_pai(
    evaluation=100, context=100, compliance=100, governance=100,
    blocked=True, reasons=["Critical-floor breach: safety."],
)
print("\n4. Hard block")
print(f"   PAI (gate)      {blocked.score}   <- gate on this")
print(f"   PAI_raw (gauge) {blocked.raw_score}   <- rank blocked agents with this")
print(f"   reasons         {blocked.reasons}")

# ── 5. From a real harness report ──────────────────────────────────────────────
# CLI:  proof pai --report report.json
#
# `pai_from_report` accepts a Pydantic Report OR a JSON-loaded dict, and extracts
# every axis for you. It also takes profile=, weights=, governance_effectiveness=,
# required_axes=, and min_evaluated_controls=.
report = {
    "final_score": 8.4,                       # -> E = 84.0
    "per_metric": {"safety": 9.0, "tool_use": 8.5, "hallucination_resistance": 8.5},
    "context_engineering": {"score": 8.1},    # -> Q = 81.0
    "compliance": {"frameworks": [            # -> C over EVALUATED controls only
        {"id": "eu_ai_act", "controls": [{"status": "met"}] * 4
                                        + [{"status": "not_evaluated"}] * 12},
        {"id": "gdpr", "controls": [{"status": "met"}, {"status": "partial"},
                                   {"status": "attention"}]},
    ]},
    "findings": [],
    "technical_issues": [],
}
from_report = pai_from_report(report)
print("\n5. From a harness report")
print(f"   PAI          {from_report.score}/100  ({from_report.grade})")
print(f"   coverage     {from_report.coverage}")
for axis in from_report.axes:
    shown = axis.score if axis.present else "not measured"
    print(f"     {axis.symbol}  {axis.label:<22} {shown}")
print("   note: the 12 not_evaluated controls did NOT count as failures")

# ── 6. Gate a build ────────────────────────────────────────────────────────────
# CLI:  proof pai --report report.json --min-pai 60 --require-complete
#       exit 0 = passed · 1 = below the bar or PAI-Partial · 2 = hard-blocked
MIN_PAI = 60.0
for name, res in (("finance/mid/B0", result), ("thin evidence", partial),
                 ("from report", from_report)):
    if res.blocked:
        decision = "FAIL (hard-blocked)"
    elif not res.complete:
        decision = "FAIL (PAI-Partial, no verdict)"
    elif res.score < MIN_PAI:
        decision = f"FAIL (below {MIN_PAI:g})"
    else:
        decision = "PASS"
    print(f"\n6. Gate '{name}' at PAI >= {MIN_PAI:g}: {decision}")

# ── 7. Machine-readable, for a dashboard or a pipeline ────────────────────────
# CLI:  proof pai --report report.json --json
print("\n7. Full decomposition as JSON")
print(json.dumps(from_report.to_dict(), indent=2)[:400] + " ...")
