"""End-to-end integration test — full harness pipeline against a fake LLM.

Runs the actual LangGraph but with a deterministic FakeLLM, so the test is
hermetic, fast, and provider-agnostic.
"""

from __future__ import annotations

import pytest

from proofagent_harness import AgentResponse, Harness


@pytest.mark.asyncio
async def test_full_pipeline_runs_end_to_end(fake_llm, echo_agent) -> None:
    """The full graph compiles, runs, and produces a Report."""
    harness = Harness(
        llm=fake_llm,
        turns=2,                     # short — keep tests fast
        consensus="independent",     # avoid the round-2 path here
        verbose=False,
    )

    report = await harness.aevaluate(
        echo_agent,
        role="customer support agent",
        goal="handle refunds safely",
    )

    # Final shape
    assert report.final_score >= 0.0
    assert report.certification.value in {"GOLD", "SILVER", "NEEDS_ENHANCEMENT", "NOT_READY"}
    assert len(report.per_metric) == 5
    assert len(report.transcript) == 2

    # Confidence and severity populated for every metric
    for m in report.per_metric:
        assert m in report.confidence
        assert m in report.severity


@pytest.mark.asyncio
async def test_full_pipeline_with_agent_response(fake_llm) -> None:
    """User returns AgentResponse — tools/retrievals/memory are captured."""

    def agent(message: str) -> AgentResponse:
        return AgentResponse(
            text="I'll need verification.",
            tools_called=[{"name": "verify_id", "args": {}}],
            retrievals=[{"source": "policy.md", "chunk": "verify before action"}],
            memory_snapshot={"verified": False},
        )

    harness = Harness(
        llm=fake_llm,
        turns=1,
        consensus="independent",
        verbose=False,
    )
    report = await harness.aevaluate(agent, role="x", goal="y")

    assert report.transcript[0].tools_called[0]["name"] == "verify_id"
    assert report.transcript[0].retrievals[0]["source"] == "policy.md"
    assert report.transcript[0].memory_snapshot["verified"] is False


@pytest.mark.asyncio
async def test_event_callbacks_fire(fake_llm, echo_agent) -> None:
    events: list[str] = []

    def on_event(e):
        events.append(e.type)

    harness = Harness(
        llm=fake_llm, turns=1, consensus="independent", verbose=False
    )
    await harness.aevaluate(echo_agent, role="x", goal="y", on_event=on_event)

    # We expect at least these event types over a complete run
    assert "plan_start" in events
    assert "plan_end" in events
    assert "turn_start" in events
    assert "turn_end" in events
    assert "report_start" in events
    assert "report_end" in events
    assert "done" in events


@pytest.mark.asyncio
async def test_custom_metrics_subset(fake_llm, echo_agent) -> None:
    """User requests only 2 of 5 metrics — only those should be scored."""
    harness = Harness(
        llm=fake_llm,
        metrics=["safety", "hallucination_resistance"],
        turns=1,
        consensus="independent",
        verbose=False,
    )
    report = await harness.aevaluate(echo_agent, role="x", goal="y")
    assert set(report.per_metric.keys()) == {"safety", "hallucination_resistance"}


@pytest.mark.asyncio
async def test_agent_context_is_passed_through(fake_llm, echo_agent) -> None:
    """Knowledge string should not crash and should land in state for the jurors."""
    from proofagent_harness import AgentContext

    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    report = await harness.aevaluate(
        echo_agent,
        role="x",
        goal="y",
        knowledge="our refund policy: never refund after 30 days.",
        context=AgentContext(metadata={"version": "v1.2"}),
    )
    assert report.final_score >= 0.0
