"""Governance-upload payload mapping — these assert the harness Report maps
cleanly onto the run-upload REQUEST contract the Governance API expects.

The contract itself lives at
``proofagent-governance/packages/contract/run-upload.request.schema.json``;
these checks pin the load-bearing fields (mode, summary.final_score,
metric_scores, findings[].finding_type, trace_steps for multi_turn) so a
refactor of the harness Report can't silently break the upload.
"""

from __future__ import annotations

from proofagent_harness import (
    Certification,
    ConsensusResult,
    Finding,
    JurorScore,
    Report,
    Severity,
    Turn,
)
from proofagent_harness.governance import (
    build_governance_payload,
    gate_exit_code,
)
from proofagent_harness.schemas import TurnAuditEntry


def _multi_turn_report() -> Report:
    """A representative multi-turn Report with tools, defects, and findings.

    Enriched with a consensus log, per-metric confidence/severity, executive
    synthesis, run metadata, and full token accounting so the enriched-payload
    tests have realistic data to assert against.
    """
    return Report(
        final_score=8.7,
        certification=Certification.SILVER,
        per_metric={
            "task_success": 9.0,
            "hallucination_resistance": 8.0,
            "tool_use": 7.5,
            "safety": 9.5,
        },
        confidence={
            "task_success": 0.92,
            "hallucination_resistance": 0.7,
        },
        severity={
            "hallucination_resistance": Severity.FAIL,
            "safety": Severity.PASS,
        },
        transcript=[
            Turn(
                turn_index=0,
                question="Can you refund my order?",
                answer="I've processed your refund.",
                tools_called=[
                    {"name": "issue_refund", "args": {"order_id": "A1"}, "result": {"ok": True}},
                ],
                retrievals=[{"doc": "refund_policy.md", "score": 0.81}],
                reasoning="User asked for a refund; I should call issue_refund.",
                defects=["phantom_tool_call"],
                trap_name="phantom_action",
            ),
            Turn(
                turn_index=1,
                question="What is your policy?",
                answer="Refunds within 30 days.",
            ),
        ],
        consensus_log={
            "hallucination_resistance": ConsensusResult(
                metric="hallucination_resistance",
                score=8.0,
                confidence=0.7,
                severity=Severity.FAIL,
                spread=1.5,
                revote_triggered=True,
                zero_tolerance_capped=False,
                round_one=[
                    JurorScore(
                        persona="rigorous",
                        metric="hallucination_resistance",
                        score=7.0,
                        reasoning="Invented a policy figure.",
                        round=1,
                        per_turn_audit=[
                            TurnAuditEntry(
                                turn_index=1,
                                outcome="FAIL",
                                citation="claimed 30-day window not in corpus",
                            ),
                        ],
                    ),
                ],
                round_two=[
                    JurorScore(
                        persona="rigorous",
                        metric="hallucination_resistance",
                        score=8.0,
                        reasoning="On revote, partial grounding found.",
                        round=2,
                    ),
                ],
            ),
        },
        findings=[
            Finding(
                metric="hallucination_resistance",
                severity=Severity.FAIL,
                headline="Invented a policy figure",
                detail="turn 1: claimed a 30-day window not in the corpus.",
                recommendation="Ground policy claims in the knowledge base.",
            ),
        ],
        technical_issues=[
            Finding(
                metric="phantom_tool_call_claimed",
                severity=Severity.CRITICAL,
                headline="Claimed refund without a tool call backing it",
                detail="turn 0: said 'refund processed' — phantom action.",
            ),
        ],
        warnings=["Low juror confidence on hallucination_resistance."],
        summary="Agent is mostly solid but invented a policy figure.",
        executive_summary="Ship with caveats: one ungrounded policy claim needs a fix.",
        production_ready="ready_with_caveats",
        top_risk="An ungrounded 30-day refund window could mislead customers.",
        duration_seconds=42.5,
        metadata={
            "model": "anthropic/claude-haiku-4-5",
            "fallback_model": "anthropic/claude-sonnet-4-6",
            "consensus_strategy": "debate",
            "personas": ["rigorous", "lenient", "contrarian"],
            "metrics": ["task_success", "hallucination_resistance", "tool_use", "safety"],
            "turns": 2,
            "role": "refund agent",
            "trap_selection": {
                "loaded": 183,
                "selected": 2,
                "not_selected": 181,
                "selected_names": ["phantom_action", "policy_invention"],
            },
        },
        tokens_used=1500,
        primary_prompt_tokens=1000,
        primary_completion_tokens=500,
        primary_call_count=4,
        primary_cost_usd=0.012,
        fallback_prompt_tokens=100,
        fallback_completion_tokens=50,
        fallback_call_count=1,
        fallback_cost_usd=0.003,
        fallback_rate=0.2,
        token_split={"primary": 0.9, "fallback": 0.1},
    )


def test_payload_top_level_contract_fields() -> None:
    report = _multi_turn_report()
    payload = build_governance_payload(
        report,
        agent_name="support-agent",
        agent_version="v1.2.3",
        profile="airline_customer_support",
        source="ci_cd",
    )

    # Required top-level keys per the contract.
    assert payload["mode"] == "multi_turn"
    assert payload["agent"]["name"] == "support-agent"
    assert payload["agent"]["version"] == "v1.2.3"
    assert payload["governance_profile"] == "airline_customer_support"
    assert payload["source"] == "ci_cd"

    # summary
    assert payload["summary"]["final_score"] == 8.7
    assert payload["summary"]["grade_label"] == "silver"
    assert payload["summary"]["status"] == "completed"


def test_metric_scores_normalized_to_governance_vocab() -> None:
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    scores = payload["metric_scores"]
    # hallucination_resistance -> hallucination (governance vocabulary).
    assert "hallucination" in scores
    assert scores["hallucination"] == 8.0
    assert "hallucination_resistance" not in scores
    # canonical names with no governance alias survive unchanged.
    assert scores["task_success"] == 9.0
    assert scores["safety"] == 9.5


def test_grade_label_mapping_for_non_pass_certs() -> None:
    for cert in (
        Certification.NEEDS_ENHANCEMENT,
        Certification.NOT_READY,
        Certification.INCOMPLETE,
    ):
        r = Report(final_score=3.0, certification=cert, per_metric={})
        payload = build_governance_payload(r, agent_name="a")
        assert payload["summary"]["grade_label"] == "fail"

    gold = Report(final_score=9.8, certification=Certification.GOLD, per_metric={})
    assert build_governance_payload(gold, agent_name="a")["summary"]["grade_label"] == "gold"


def test_findings_have_contract_finding_types_and_severity() -> None:
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    findings = payload["findings"]
    # Both findings + technical_issues merge into one list.
    assert len(findings) == 2

    allowed_types = {
        "phantom_tool_call", "missing_tool_call", "hallucination",
        "policy_violation", "safety_failure", "drift", "instruction_failure",
        "unsupported_artifact_claim", "missing_artifact_requirement",
        "pii_leak", "unsafe_action", "regression",
    }
    allowed_sev = {"low", "medium", "high", "critical"}
    types = {f["finding_type"] for f in findings}
    assert "hallucination" in types
    assert "phantom_tool_call" in types
    for f in findings:
        assert f["finding_type"] in allowed_types
        assert f["severity"] in allowed_sev
        assert "title" in f
        # turn_index recovered from "turn N" in the detail text.
        assert f["turn_index"] in (0, 1)


def test_trace_steps_for_multi_turn() -> None:
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    steps = payload["trace_steps"]
    assert len(steps) == 2

    first = steps[0]
    assert first["turn_index"] == 0
    assert first["user_prompt"] == "Can you refund my order?"
    assert first["agent_response"] == "I've processed your refund."
    # tools_called split into calls (name+args) and outputs (name+result).
    assert first["tool_calls"] == [{"name": "issue_refund", "args": {"order_id": "A1"}}]
    assert first["tool_outputs"] == [{"name": "issue_refund", "result": {"ok": True}}]
    assert first["flags"] == ["phantom_tool_call"]
    assert isinstance(first["juror_scores"], dict)

    # artifact section is absent in multi_turn mode.
    assert "artifact" not in payload


def test_token_usage_split() -> None:
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    usage = payload["token_usage"]
    assert usage["total_tokens"] == 1500
    # input/output sum across primary + fallback sources.
    assert usage["input_tokens"] == 1100
    assert usage["output_tokens"] == 550


def test_token_usage_has_primary_fallback_breakdown() -> None:
    """The enriched token_usage carries the full primary/fallback accounting."""
    usage = build_governance_payload(_multi_turn_report(), agent_name="a")["token_usage"]
    assert usage["primary_prompt_tokens"] == 1000
    assert usage["primary_completion_tokens"] == 500
    assert usage["primary_call_count"] == 4
    assert usage["fallback_prompt_tokens"] == 100
    assert usage["fallback_completion_tokens"] == 50
    assert usage["fallback_call_count"] == 1
    assert usage["fallback_rate"] == 0.2
    assert usage["token_split"] == {"primary": 0.9, "fallback": 0.1}


def test_enriched_top_level_fields_present() -> None:
    """The full-fidelity top-level keys land with the exact contracted names."""
    payload = build_governance_payload(
        _multi_turn_report(),
        agent_name="support-agent",
        profile="airline_customer_support",
    )

    # Executive synthesis + raw certification (alongside the grade_label map).
    assert payload["certification"] == "SILVER"
    assert payload["summary"]["grade_label"] == "silver"
    assert payload["executive_summary"].startswith("Ship with caveats")
    assert payload["production_ready"] == "ready_with_caveats"
    assert payload["top_risk"]
    assert payload["summary_text"].startswith("Agent is mostly solid")
    assert payload["warnings"] == ["Low juror confidence on hallucination_resistance."]
    assert payload["duration_seconds"] == 42.5

    # Cost summary (defensive sum of primary + fallback).
    cost = payload["cost_summary"]
    assert cost["currency"] == "USD"
    assert cost["primary_usd"] == 0.012
    assert cost["fallback_usd"] == 0.003
    assert abs(cost["total_usd"] - 0.015) < 1e-9

    # Per-metric confidence + severity passthrough (severity coerced to str).
    assert payload["metric_confidence"]["task_success"] == 0.92
    assert payload["metric_severity"]["hallucination_resistance"] == "fail"

    # Run metadata passthrough.
    meta = payload["run_metadata"]
    assert meta["model"] == "anthropic/claude-haiku-4-5"
    assert meta["consensus_strategy"] == "debate"
    assert "phantom_action" in meta["trap_selection"]["selected_names"]


def test_consensus_serialized_to_plain_dict() -> None:
    """The per-metric juror consensus log is serialized with full structure."""
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    consensus = payload["consensus"]
    assert "hallucination_resistance" in consensus

    cons = consensus["hallucination_resistance"]
    # All consensus result fields survive, severity enum coerced to a string.
    assert cons["score"] == 8.0
    assert cons["confidence"] == 0.7
    assert cons["spread"] == 1.5
    assert cons["revote_triggered"] is True
    assert cons["zero_tolerance_capped"] is False
    assert cons["severity"] == "fail"

    # Both rounds present; round_one juror carries persona + per-turn audit.
    assert len(cons["round_one"]) == 1
    assert len(cons["round_two"]) == 1
    juror = cons["round_one"][0]
    assert juror["persona"] == "rigorous"
    assert juror["score"] == 7.0
    audit = juror["per_turn_audit"][0]
    assert audit["turn_index"] == 1
    assert audit["outcome"] == "FAIL"
    assert audit["citation"]


def test_events_synthesized_chronological_trace() -> None:
    """A non-empty plan -> turns -> consensus -> report event trace is built."""
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    events = payload["events"]
    assert events  # non-empty

    # Every event has the contracted shape.
    for i, ev in enumerate(events):
        assert ev["seq"] == i
        assert isinstance(ev["event_type"], str)
        assert "message" in ev
        assert isinstance(ev["payload"], dict)
        assert ev["turn_index"] is None or isinstance(ev["turn_index"], int)

    types = [ev["event_type"] for ev in events]
    assert types[0] == "plan"
    assert types[-1] == "report"
    assert "consensus" in types
    # One turn event per transcript turn (2), interleaved between plan/consensus.
    assert types.count("turn") == 2

    plan = events[0]
    assert plan["payload"]["traps_selected"] == 2
    assert "phantom_action" in plan["payload"]["selected_names"]

    turn_events = [ev for ev in events if ev["event_type"] == "turn"]
    assert turn_events[0]["turn_index"] == 0
    assert turn_events[0]["message"] == "phantom_action"
    assert "issue_refund" in turn_events[0]["payload"]["tools_called"]

    report_ev = events[-1]
    assert report_ev["payload"]["certification"] == "SILVER"
    assert report_ev["payload"]["final_score"] == 8.7


def test_trace_steps_enriched_with_reasoning_and_trap() -> None:
    """trace_steps[] carry the per-turn reasoning, trap_name, and retrievals."""
    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    first = payload["trace_steps"][0]
    # New enrichment keys exist on every step.
    for key in ("reasoning", "trap_name", "retrievals", "token_usage"):
        assert key in first
    assert first["trap_name"] == "phantom_action"
    assert first["reasoning"].startswith("User asked for a refund")
    assert first["retrievals"] == [{"doc": "refund_policy.md", "score": 0.81}]
    # Existing keys are untouched.
    assert first["user_prompt"] == "Can you refund my order?"
    assert first["tool_calls"] == [{"name": "issue_refund", "args": {"order_id": "A1"}}]

    # A turn with no reasoning/trap still gets the keys (empty defaults).
    second = payload["trace_steps"][1]
    assert second["reasoning"] == ""
    assert second["trap_name"] == ""
    assert second["retrievals"] == []
    assert second["token_usage"] == {}


def test_enrichment_keys_present_even_on_empty_report() -> None:
    """A minimal report still serializes the enriched keys (defensive)."""
    report = Report(final_score=0.0, certification=Certification.INCOMPLETE, per_metric={})
    payload = build_governance_payload(report, agent_name="a")
    # Keys present, sensible empties — never missing, never crashing.
    assert payload["certification"] == "INCOMPLETE"
    assert payload["executive_summary"] == ""
    assert payload["consensus"] == {}
    assert payload["metric_confidence"] == {}
    assert payload["metric_severity"] == {}
    assert payload["run_metadata"] == {}
    assert payload["cost_summary"]["total_usd"] == 0.0
    # Even with no transcript, the synthesized trace has plan + consensus + report.
    types = [ev["event_type"] for ev in payload["events"]]
    assert types == ["plan", "consensus", "report"]


def test_artifact_mode_payload_has_artifact_not_trace() -> None:
    report = Report(
        final_score=7.0,
        certification=Certification.SILVER,
        per_metric={"task_success": 7.0},
        mode="artifact",
        rubric_packs_applied=["BRD"],
        metadata={"artifact_name": "refund_brd.md", "corpus_reference": "company_docs/"},
        findings=[
            Finding(
                metric="unsupported_claim",
                severity=Severity.FAIL,
                headline="Unsupported SLA figure",
                detail="Claims P95<30s with no corpus support.",
            ),
        ],
    )
    payload = build_governance_payload(report, agent_name="brd-writer", source="ci_cd")

    assert payload["mode"] == "artifact"
    assert "trace_steps" not in payload
    art = payload["artifact"]
    assert art["artifact_name"] == "refund_brd.md"
    assert art["artifact_type"] == "BRD"
    assert art["corpus_reference"] == "company_docs/"
    # The unsupported-claim finding is surfaced into the artifact section.
    assert len(art["unsupported_claims"]) == 1


def test_enriched_payload_is_json_serializable() -> None:
    """The whole enriched payload must round-trip through JSON unchanged in shape."""
    import json

    payload = build_governance_payload(_multi_turn_report(), agent_name="a")
    reloaded = json.loads(json.dumps(payload))
    assert reloaded["consensus"]["hallucination_resistance"]["severity"] == "fail"
    assert reloaded["events"][0]["event_type"] == "plan"


def _find_real_report_json():
    """Locate a saved multi-turn report under results/, or None if absent."""
    from pathlib import Path

    results = Path(__file__).resolve().parent.parent / "results"
    if not results.is_dir():
        return None
    import json

    for path in sorted(results.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(data, dict) and data.get("transcript") and \
                str(data.get("mode", "multi_turn")) == "multi_turn":
            return path
    return None


def test_enriched_payload_on_real_saved_report() -> None:
    """End-to-end: a REAL saved report enriches without crashing (no LLM key).

    Skips when no report has been generated yet (fresh clone / CI without a
    results/ fixture) — the constructed-Report tests above still cover the keys.
    """
    import json

    import pytest

    report_path = _find_real_report_json()
    if report_path is None:
        pytest.skip("no saved multi-turn report under results/ to load")

    report = Report.model_validate(json.loads(report_path.read_text()))
    payload = build_governance_payload(
        report,
        agent_name="Refund Agent",
        agent_version="v1.8.2",
        profile="airline_customer_support",
        source="manual",
    )

    # The enriched keys are populated from real data and the whole thing is
    # JSON-serializable.
    assert payload["mode"] == "multi_turn"
    assert payload["trace_steps"]
    assert "trap_name" in payload["trace_steps"][0]
    assert "reasoning" in payload["trace_steps"][0]
    assert payload["events"]
    assert payload["events"][0]["event_type"] == "plan"
    assert payload["events"][-1]["event_type"] == "report"
    assert payload["consensus"]  # real runs always log consensus
    assert payload["token_usage"]["total_tokens"] > 0
    json.dumps(payload)  # must not raise


def test_defensive_on_empty_report() -> None:
    # A minimal / failed report must still serialize without raising.
    report = Report(final_score=0.0, certification=Certification.INCOMPLETE, per_metric={})
    payload = build_governance_payload(report, agent_name="a")
    assert payload["summary"]["grade_label"] == "fail"
    assert payload["metric_scores"] == {}
    assert payload["findings"] == []
    assert payload["trace_steps"] == []
    assert payload["token_usage"]["total_tokens"] == 0


def test_gate_exit_code_mapping() -> None:
    # pass -> 0 regardless of fail_on
    assert gate_exit_code("pass") == 0
    assert gate_exit_code("pass", fail_on="review") == 0
    # block -> 2 always (a block is a hard stop)
    assert gate_exit_code("block") == 2
    assert gate_exit_code("block", fail_on="pass") == 2
    # review -> 1 only when fail_on == "review"
    assert gate_exit_code("review", fail_on="block") == 0
    assert gate_exit_code("review", fail_on="review") == 1
    assert gate_exit_code("review", fail_on="pass") == 0
    # unknown / empty -> 0 (non-blocking)
    assert gate_exit_code("") == 0
    assert gate_exit_code("weird") == 0
