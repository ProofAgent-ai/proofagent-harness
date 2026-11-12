"""Readiness withheld for a missing axis: the harness still reports a labelled partial value."""

from proofagent_harness.per_export import partial_readiness


def _rec(axes, value=None, capped=False):
    return {"scores": {
        "scoring_profile": {"id": "eio-agents.reference-scoring", "version": "0.3.1"},
        "axes": [{"axis": f"eio.axis.{a}", "value": v} for a, v in axes.items()],
        "readiness": {"value": value, "status": "WITHHELD" if value is None else "MEASURED",
                      "cap": {"applied": capped}},
    }}


def test_partial_readiness_averages_the_measured_axes_and_names_the_missing_one() -> None:
    part = partial_readiness(_rec({"context": None, "behaviour": 50.7143, "compliance": 86.4286, "governance": 25.0}))
    assert part["missing"] == ["context"] and part["note"] == "context not measured"
    assert part["measured"] == ["behaviour", "compliance", "governance"]
    assert 47.0 < part["value"] < 49.0          # weighted geometric mean of 50.7, 86.4, 25


def test_cap_applies_and_measured_readiness_is_left_alone() -> None:
    part = partial_readiness(_rec({"context": None, "behaviour": 90, "compliance": 90, "governance": 90}, capped=True))
    assert part["value"] == 49.0 and part["capped"]
    assert partial_readiness(_rec({"context": 80, "behaviour": 80, "compliance": 80, "governance": 80}, value=80)) is None
    assert partial_readiness(_rec({"context": None, "behaviour": None, "compliance": None, "governance": None})) is None


def test_context_gaps_reach_the_per_as_verified_absences():
    """The PER carried 0 findings while the report listed context gaps: the export never sent EIO a context
    assessment. Gaps are now declared only where NO checklist term occurs in ANY embedded file, which EIO
    re-verifies, so a false gap cannot be recorded."""
    from proofagent_harness import per_export as X
    bundle = {"sources": {"context_artifacts": [{"artifact_kind": "system_prompt", "name": "p", "sha256": "x",
                                                 "code_points": 20, "embedded": True, "data_class": "x"}],
                          "context_texts": {"p": "Never share PII. Escalate to a supervisor."}}}
    n = X._declare_context_gaps(bundle, None)
    gaps = bundle["context_assessment"]["gaps"]
    assert n == len(gaps) > 0
    hit = {(g["criterion"], g["control"]) for g in gaps}
    assert ("eio.context.guardrail-coverage", "personal-data") not in hit      # "PII" is present
    assert ("eio.context.guardrail-coverage", "payment-instrument") in hit     # nothing about payments
    assert X._declare_context_gaps({"sources": {}}, None) == 0


def test_report_verdict_follows_the_per_release_decision():
    """A report read SHIP / GOLD beside a PER holding the release for REVIEW. The PER is authoritative:
    the report may be stricter, never looser."""
    import json
    from pathlib import Path
    from proofagent_harness import audit
    from proofagent_harness.cli import _carry_per_release
    from proofagent_harness.schemas import Report
    src = Path(__file__).parent.parent / "examples/multi/results/ab_local6/run.json"
    if not src.exists():
        import pytest
        pytest.skip("sample run not present")
    r = Report.model_validate(json.loads(src.read_text()))
    _carry_per_release(r, {"release_recommendation": {"state": "REVIEW", "decisive": [
        {"id": "profile.min_score", "expected": ">= 95.0", "observed": "91.4"}]}})
    assert audit.decision(r)["verdict"] == "HOLD FOR REVIEW"
    _carry_per_release(r, {"release_recommendation": {"state": "BLOCK", "decisive": []}})
    assert audit.decision(r)["verdict"] == "DO NOT SHIP"
