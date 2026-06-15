"""Schema sanity checks — these are part of the public contract."""

from __future__ import annotations

from proofagent_harness import (
    CANONICAL_METRICS,
    AgentContext,
    AgentResponse,
    Certification,
    Report,
    Severity,
)


def test_canonical_metrics_are_six() -> None:
    assert len(CANONICAL_METRICS) == 6
    assert "task_success" in CANONICAL_METRICS
    assert "hallucination_resistance" in CANONICAL_METRICS
    assert "safety" in CANONICAL_METRICS
    assert "instruction_following" in CANONICAL_METRICS
    assert "manipulation_resistance" in CANONICAL_METRICS
    assert "tool_use" in CANONICAL_METRICS  # v0.5.0 — 6th metric


def test_metric_aliases_resolve_to_canonical() -> None:
    from proofagent_harness.schemas import canonicalize_metric

    assert canonicalize_metric("hallucination") == "hallucination_resistance"
    assert canonicalize_metric("factuality") == "hallucination_resistance"
    assert canonicalize_metric("faithfulness") == "hallucination_resistance"
    assert canonicalize_metric("groundedness") == "hallucination_resistance"
    # canonical names pass through unchanged
    assert canonicalize_metric("safety") == "safety"
    assert canonicalize_metric("hallucination_resistance") == "hallucination_resistance"


def test_agent_response_minimum() -> None:
    r = AgentResponse(text="hello")
    assert r.text == "hello"
    assert r.tools_called == []
    assert r.retrievals == []


def test_agent_response_full() -> None:
    r = AgentResponse(
        text="hi",
        tools_called=[{"name": "t", "args": {}}],
        retrievals=[{"source": "doc.md", "chunk": "..."}],
        memory_snapshot={"x": 1},
        reasoning="because",
    )
    assert r.tools_called[0]["name"] == "t"
    assert r.retrievals[0]["source"] == "doc.md"
    assert r.memory_snapshot["x"] == 1
    assert r.reasoning == "because"


def test_agent_context_all_optional() -> None:
    ctx = AgentContext()
    assert ctx.system_prompt is None
    assert ctx.knowledge is None
    assert ctx.tools is None
    assert ctx.memory is None
    assert ctx.metadata == {}


def test_report_serializes_round_trip() -> None:
    r = Report(
        final_score=7.4,
        certification=Certification.SILVER,
        per_metric={"task_success": 8, "hallucination_resistance": 7, "safety": 8, "instruction_following": 7, "manipulation_resistance": 7},
        confidence={"task_success": 0.9},
        severity={"task_success": Severity.PASS},
        summary="ok",
    )
    s = r.to_json()
    assert "final_score" in s
    assert "SILVER" in s


def test_transcript_as_memory_pairs_q_and_a() -> None:
    from proofagent_harness import Turn

    r = Report(
        final_score=5,
        certification=Certification.NOT_READY,
        per_metric={},
        transcript=[Turn(turn_index=1, question="q1", answer="a1", trap_name="x")],
    )
    mem = r.transcript_as_memory()
    assert mem == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
