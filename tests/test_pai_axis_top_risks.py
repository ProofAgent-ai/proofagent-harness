"""One risk per axis, ranked by consequence — and no overall score in the verdict prose.

WHY BOTH LIVE IN ONE FILE. They are two halves of the same defect. An agent card showed
"49% · Grade F · Critical · BLOCKED" beside verdict text reading "The agent scored 65%" — two
different percentages for one run, because the prose is written by the reporter from the
BEHAVIOURAL axis while the headline is the four-axis index. The prose now states no overall score
(the index cannot be quoted there even in principle: it is computed FROM the finished report), and
the per-axis ranking below supplies the "which layer is failing" that the number alone cannot.
"""

from __future__ import annotations

from proofagent_harness.scoring.pai import Axis, _sorted_top_risks


def _axis(key: str, label: str, score: float, sub: list[dict]) -> Axis:
    return Axis(key=key, label=label, score=score, weight=1.0, present=True, sub=sub)


def test_the_top_risk_is_the_worst_component_on_the_axis() -> None:
    a = _axis("context", "Context engineering", 56.0, [
        {"name": "Role Clarity", "score": 80.0, "severity": "info"},
        {"name": "Injection Hardening", "score": 30.0, "severity": "fail"},
        {"name": "Grounding Sufficiency", "score": 40.0, "severity": "fail"},
    ])
    assert a.top_risk is not None
    assert a.top_risk["name"] == "Injection Hardening"
    assert a.top_risk["symbol"] == "Q"
    assert a.top_risk["axis_label"] == "Context engineering"


def test_severity_outranks_a_lower_raw_score() -> None:
    """A component nobody considers serious is not the headline risk, even at a lower number.
    Severity is the reviewer's judgement about consequence; the score alone is not."""
    a = _axis("evaluation", "Behavioral evaluation", 82.0, [
        {"name": "Token Efficiency", "score": 20.0, "severity": "info"},
        {"name": "Task Success", "score": 45.0, "severity": "critical"},
    ])
    assert a.top_risk["name"] == "Task Success"


def test_an_axis_with_no_components_has_no_top_risk() -> None:
    """Absence must read as absence — inventing a risk for an unmeasured axis is the
    not-tested-is-not-a-pass mistake in the other direction."""
    assert _axis("compliance", "Framework compliance", 70.0, []).top_risk is None
    assert _axis("governance", "Governance", 70.0,
                 [{"name": "Oversight", "score": None, "severity": "info"}]).top_risk is None


def test_the_critical_risk_leads_even_when_its_axis_scored_best() -> None:
    """The regression this ordering exists for.

    Measured on a real run: behavioural evaluation was the STRONGEST axis at 82, and it held the
    only CRITICAL component (task success, 11%). Sorting by axis score put that critical item last
    of four, so a reader scanning a verdict downward met it at the bottom.
    """
    axes = [
        _axis("context", "Context engineering", 56.0,
              [{"name": "Injection Hardening", "score": 30.0, "severity": "fail"}]),
        _axis("governance", "Governance", 62.0,
              [{"name": "Open findings", "score": 30.0, "severity": "fail"}]),
        _axis("compliance", "Framework compliance", 67.3,
              [{"name": "EU GDPR", "score": 55.0, "severity": "warn"}]),
        _axis("evaluation", "Behavioral evaluation", 82.4,
              [{"name": "Task Success", "score": 10.9, "severity": "critical"}]),
    ]
    ordered = _sorted_top_risks(axes)
    assert [r["symbol"] for r in ordered] == ["E", "Q", "G", "C"]
    assert ordered[0]["severity"] == "critical"


def test_equal_severities_break_toward_the_weaker_axis() -> None:
    axes = [
        _axis("governance", "Governance", 62.0,
              [{"name": "Open findings", "score": 30.0, "severity": "fail"}]),
        _axis("context", "Context engineering", 56.0,
              [{"name": "Injection Hardening", "score": 30.0, "severity": "fail"}]),
    ]
    assert [r["symbol"] for r in _sorted_top_risks(axes)] == ["Q", "G"]


def test_an_absent_axis_is_left_out_entirely() -> None:
    present = _axis("context", "Context engineering", 56.0,
                    [{"name": "Injection Hardening", "score": 30.0, "severity": "fail"}])
    absent = Axis(key="compliance", label="Framework compliance", score=None, weight=1.0,
                  present=False, sub=[{"name": "EU GDPR", "score": 55.0, "severity": "warn"}])
    assert [r["symbol"] for r in _sorted_top_risks([present, absent])] == ["Q"]


def test_the_serialized_result_carries_the_ranking() -> None:
    """The dashboard reads `pai.axis_top_risks`; it must survive to_dict()."""
    import json
    import pathlib

    from proofagent_harness.schemas import Report
    from proofagent_harness.scoring.pai import pai_from_report

    fixture = pathlib.Path("/tmp/fleet_out/support_concierge.json")
    if not fixture.exists():  # pragma: no cover - fixture is developer-local
        return
    d = pai_from_report(Report.model_validate(json.loads(fixture.read_text()))).to_dict()
    assert "axis_top_risks" in d
    assert d["axis_top_risks"], "a scored run produced no per-axis risks"
    for r in d["axis_top_risks"]:
        assert {"axis", "symbol", "axis_label", "name", "score", "severity"} <= set(r)
    # Every axis dict carries its own, so a renderer showing one axis needs no second lookup.
    assert any(a.get("top_risk") for a in d["axes"])


# ── the prose half ───────────────────────────────────────────────────────────


def test_the_verdict_prompt_forbids_an_overall_score() -> None:
    """Pinned on the prompt text because that is where the contradiction was authored."""
    import inspect

    from proofagent_harness.agents import reporter

    src = inspect.getsource(reporter._generate_executive_synthesis)
    assert "Do NOT state an overall score" in src
    # The behavioural score must not be handed to the model at all — being told not to quote a
    # number it can see is weaker than not seeing it.
    assert "Final score: {final_score" not in src
    assert 'f"Final score {final_score' not in src, (
        "the deterministic fallback still states the behavioural score, so the no-LLM path "
        "reintroduces the contradiction")
