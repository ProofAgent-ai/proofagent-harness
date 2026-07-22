"""Tests for the optional context-engineering assessment (v0.7.0).

The assessment is ADDITIVE and OPT-IN: it must never touch scoring, must be a
no-op when there's no context or the LLM is unavailable, and must travel in the
Report + governance payload without breaking either.
"""

from __future__ import annotations

import json

from proofagent_harness import AgentContext, Harness
from proofagent_harness.context_engineering import CRITERIA, assess_context_engineering
from proofagent_harness.governance import build_governance_payload
from proofagent_harness.schemas import Certification, Report


def _report(**over):
    base = {
        "final_score": 8.0,
        "certification": Certification.SILVER,
        "per_metric": {"safety": 9.0, "tool_use": 8.0},
    }
    base.update(over)
    return Report(**base)


# ── no-op safety ────────────────────────────────────────────────────────────

def test_no_context_is_noop():
    assert assess_context_engineering(context=None) == {}


def test_empty_context_is_noop():
    # An AgentContext with no system_prompt and no tools → nothing to grade.
    assert assess_context_engineering(context=AgentContext()) == {}


def test_no_litellm_call_when_nothing_to_assess(monkeypatch):
    # Guard: the no-op path must not even reach litellm.
    import proofagent_harness.context_engineering as ce

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("litellm should not be called for an empty context")

    monkeypatch.setattr("litellm.completion", _boom, raising=False)
    assert ce.assess_context_engineering(context=AgentContext()) == {}
    assert called["n"] == 0


# ── parse + normalize ─────────────────────────────────────────────────────────

def _fake_completion(payload):
    import types

    def _fn(**_kwargs):
        msg = types.SimpleNamespace(content=json.dumps(payload))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    return _fn


def test_assessment_parses_and_normalizes(monkeypatch):
    # Drop the last criterion to prove a missing criterion defaults to 0 (no crash).
    crit = [{"id": cid, "score": 7} for cid, _ in CRITERIA][:-1]
    payload = {
        "criteria": crit,
        "findings": [
            {"title": "Vague role", "problem": "no scope", "fix": "state scope", "token_impact": "cut"},
            {"title": "Bloat", "problem": "repeated", "fix": "dedupe", "token_impact": "BOGUS"},
        ],
        "token_savings_estimate": 1800,
        "summary": "weak setup",
    }
    monkeypatch.setattr("litellm.completion", _fake_completion(payload), raising=False)

    # Context large enough that the 1800-token estimate is below the measured
    # cap (the estimate can never exceed the size of the supplied context).
    out = assess_context_engineering(
        context=AgentContext(system_prompt="You are a helpful assistant. " * 300, tools=[{"name": "t"}]),
    )
    assert out["generated"] is True
    # Always exactly the fixed criteria set (model can't add/drop).
    assert len(out["sub_criteria"]) == len(CRITERIA)
    assert {s["id"] for s in out["sub_criteria"]} == {cid for cid, _ in CRITERIA}
    assert out["grade"] in ("strong", "adequate", "weak")
    assert isinstance(out["score"], float)
    # Invalid token_impact is normalized to "neutral".
    impacts = {f["title"]: f["token_impact"] for f in out["findings"]}
    assert impacts["Vague role"] == "cut"
    assert impacts["Bloat"] == "neutral"
    assert out["token_savings_estimate"] == 1800
    # Measured denominator + reclaimable share of the context.
    assert out["context_tokens"] > 0
    assert out["token_savings_pct"] == round(100.0 * 1800 / out["context_tokens"], 1)
    # Savings can never exceed the measured context size.
    small = assess_context_engineering(
        context=AgentContext(system_prompt="short prompt", tools=[]),
    )
    if small.get("generated"):
        assert small["token_savings_estimate"] <= small["context_tokens"]


def test_malformed_response_is_noop(monkeypatch):
    import types

    def _bad(**_kwargs):
        msg = types.SimpleNamespace(content="NOT JSON")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr("litellm.completion", _bad, raising=False)
    assert assess_context_engineering(context=AgentContext(system_prompt="x")) == {}


# ── additive Report + payload (the dashboard-safety guarantee) ─────────────────

def test_report_field_defaults_empty_and_serializes():
    r = _report()
    assert r.context_engineering == {}
    assert '"context_engineering"' in r.to_json()


def test_governance_payload_is_additive():
    # Off → empty dict, key present (additive on an additionalProperties:true contract).
    p_off = build_governance_payload(_report(), agent_name="x")
    assert p_off["context_engineering"] == {}
    # On → the assessment dict travels verbatim.
    ce = {"score": 6.2, "grade": "weak", "sub_criteria": [], "findings": [], "generated": True}
    p_on = build_governance_payload(_report(context_engineering=ce), agent_name="x")
    assert p_on["context_engineering"]["score"] == 6.2
    assert p_on["context_engineering"]["generated"] is True


# ── flag threading (off by default; never enters scoring) ──────────────────────

def test_flag_threads_into_state():
    h = Harness(llm="gpt-4.1-mini")
    state_off = h._build_initial_state(
        agent=lambda m: m, role="r", business_case="", goal="", knowledge=None,
        context=AgentContext(system_prompt="s"), on_event=lambda e: None,
    )
    assert state_off["assess_context"] is False  # default off
    state_on = h._build_initial_state(
        agent=lambda m: m, role="r", business_case="", goal="", knowledge=None,
        context=AgentContext(system_prompt="s"), on_event=lambda e: None,
        assess_context=True,
    )
    assert state_on["assess_context"] is True


def test_context_engineering_not_in_per_metric():
    # The assessment must never leak into the scored metrics.
    r = _report(context_engineering={"score": 3.0, "generated": True})
    assert "context_engineering" not in r.per_metric
    assert "context_engineering" not in r.confidence


# ── graph-propagation regression (the real bug: assess_context was dropped) ───

async def test_assess_context_reaches_reporter_through_the_graph(fake_llm, echo_agent, monkeypatch):
    """assess_context=True must survive LangGraph state propagation and trigger
    the assessment in reporter_node. (`assess_context` is a declared
    HarnessState channel — an undeclared key is silently dropped between nodes,
    which is exactly the bug this guards against.)"""
    calls = {"n": 0}

    def _stub(*, context, **_kw):
        calls["n"] += 1
        return {"score": 7.0, "grade": "adequate", "generated": True, "_stub": True}

    monkeypatch.setattr(
        "proofagent_harness.context_engineering.assess_context_engineering", _stub
    )
    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    report = await harness.aevaluate(
        echo_agent,
        role="support",
        goal="help",
        context=AgentContext(system_prompt="You are a support agent.", tools=[{"name": "lookup"}]),
        assess_context=True,
    )
    assert calls["n"] == 1, "assessment never ran — assess_context was dropped before the reporter"
    assert report.context_engineering.get("_stub") is True


async def test_assess_context_off_skips_the_assessment(fake_llm, echo_agent, monkeypatch):
    calls = {"n": 0}

    def _stub(**_kw):
        calls["n"] += 1
        return {"generated": True}

    monkeypatch.setattr(
        "proofagent_harness.context_engineering.assess_context_engineering", _stub
    )
    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    report = await harness.aevaluate(
        echo_agent, role="x", goal="y",
        context=AgentContext(system_prompt="s"),  # assess_context defaults to False
    )
    assert calls["n"] == 0
    assert report.context_engineering == {}


async def test_compliance_off_by_default(fake_llm, echo_agent):
    """Compliance assessment is OFF by default (moved behind --assess-compliance) —
    a plain run ships NO compliance section, so governance renders those frameworks
    as a neutral 'not assessed' rather than a misleading ASSESSED/0."""
    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    report = await harness.aevaluate(echo_agent, role="x", goal="y")
    assert report.compliance == {}


async def test_compliance_assessment_reaches_report_when_enabled(
    fake_llm, echo_agent, monkeypatch
):
    """With assess_compliance=True, the post-jury compliance_assessor node maps the run
    to the SELECTED frameworks (per-control status + why/proof/fix) and its output
    reaches the Report via the declared `compliance` graph channel."""
    # The node reuses the reporter's _run_json_llm bridge — stub it with a verdict.
    monkeypatch.setattr(
        "proofagent_harness.agents.reporter._run_json_llm",
        lambda *a, **k: {"frameworks": [{"id": "iso_42001", "summary": "gaps", "controls": [
            {"id": "a2", "status": "attention",
             "problem": ["The agent shipped changes without an approval gate"],
             "proof": "bypassed human review at turn 4",
             "fix": ["Require a human-approval step before deploy actions"]},
        ]}]},
    )
    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    report = await harness.aevaluate(
        echo_agent, role="x", goal="y",
        assess_compliance=True, compliance_frameworks=["iso_42001"],
    )
    assert report.compliance.get("generated") is True
    fw = report.compliance["frameworks"][0]
    assert fw["id"] == "iso_42001"
    a2 = next(c for c in fw["controls"] if c["id"] == "a2")
    assert a2["status"] == "attention"
    assert a2["problem"] and a2["proof"] and a2["fix"]
