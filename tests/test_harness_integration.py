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


@pytest.mark.asyncio
async def test_sync_evaluate_works_inside_a_running_event_loop(fake_llm, echo_agent) -> None:
    """The sync `.evaluate()` must work when called from inside an active loop
    (i.e. from a Jupyter notebook). Without the threading fallback it would
    crash with 'asyncio.run() cannot be called from a running event loop'.
    """
    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    # We're inside an `async def` test — there is a running loop right here.
    report = harness.evaluate(echo_agent, role="x", goal="y")
    assert report.final_score >= 0.0
    assert len(report.per_metric) == 5


@pytest.mark.asyncio
async def test_preflight_check_fails_fast_when_llm_unreachable(always_error_llm, echo_agent) -> None:
    """If the Harness LLM can't authenticate, the pre-flight check raises
    BEFORE any planning, conducting, or scoring work happens. No misleading
    scorecard is ever produced.
    """
    from proofagent_harness.harness import LLMNotConfiguredError

    harness = Harness(
        llm=always_error_llm, turns=8, consensus="delphi", verbose=False
    )

    with pytest.raises(LLMNotConfiguredError) as exc_info:
        await harness.aevaluate(echo_agent, role="x", goal="y")

    msg = str(exc_info.value)
    assert "pre-flight check failed" in msg
    assert "Harness LLM" in msg
    # Must mention the right env var for the model id (anthropic/openai/gemini)
    # — for the FakeLLM with model='fake/test', the generic hint is fine
    assert "env var" in msg.lower() or "API key" in msg


def test_juror_system_prompt_includes_calibration_discipline() -> None:
    """The anti-plateau-bias discipline must be in every juror's system prompt.

    Regression test for LLM-as-judge plateau bias: jurors clustering scores
    around 8-9 and refusing to award 10 (looks "overconfident") or 0-3
    (looks "harsh"). The discipline block tells them to score what they see.
    """
    from proofagent_harness.agents.juror import _build_system_prompt
    from proofagent_harness.loaders import load_personas, load_skills, get_skill

    skills = load_skills()
    persona = load_personas(["rigorous"])[0]
    rubric_skill = get_skill(skills, "score_safety")
    assert rubric_skill is not None
    state = {"context": None, "knowledge_text": ""}
    prompt = _build_system_prompt(
        persona=persona,
        metric="safety",
        rubric=rubric_skill.body,
        state=state,  # type: ignore[arg-type]
        round_num=1,
    )

    # The four anti-bias anchors must all be present
    assert "Plateau bias" in prompt
    assert "Politeness bias" in prompt
    assert "Uniformity bias" in prompt
    assert "Same-model recognition bias" in prompt        # NEW
    # 10/10 must be framed as rare
    assert "RARE" in prompt
    assert "top ~5%" in prompt
    # 8 must be framed as production baseline (not 9)
    assert "is 8/10, not 10/10" in prompt
    # And the score-justification rule
    assert "what would push this from N to N+1" in prompt


@pytest.mark.asyncio
async def test_preflight_check_emits_setup_events(fake_llm, echo_agent) -> None:
    """Verify the pre-flight check fires `setup_start` then `setup_done`
    events, in that order, before any plan_start event.
    """
    events: list[str] = []

    def on_event(e):
        events.append(e.type)

    harness = Harness(
        llm=fake_llm, turns=1, consensus="independent", verbose=False
    )
    await harness.aevaluate(echo_agent, role="x", goal="y", on_event=on_event)

    assert "setup_start" in events
    assert "setup_done" in events
    # setup must come before any planning
    assert events.index("setup_start") < events.index("plan_start")
    assert events.index("setup_done") < events.index("plan_start")
