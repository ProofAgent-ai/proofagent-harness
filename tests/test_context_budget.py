"""Tests for the context-window safety net.

The harness must not silently overflow the LLM's context. When inputs exceed
the budget it:
  - drops oldest transcript turns first
  - trims oversized fields with head + tail (preserves both ends)
  - emits a `context_truncated` event so the user knows it happened
"""

from __future__ import annotations

import pytest

from proofagent_harness import Harness, Trap, Turn
from proofagent_harness.context_budget import (
    char_budget_for,
    detect_context_tokens,
    truncate_field,
    truncate_transcript,
)


# ─── primitives ───────────────────────────────────────────────────────────


def test_truncate_field_preserves_short_text() -> None:
    text = "hello world"
    out, was = truncate_field(text, budget_chars=1000)
    assert out == text
    assert was is False


def test_truncate_field_trims_long_text_with_marker() -> None:
    text = "X" * 10_000
    out, was = truncate_field(text, budget_chars=1_000, label="kb")
    assert was is True
    assert "[kb:" in out
    assert "chars omitted" in out
    assert len(out) <= 1_100  # within budget plus small marker overhead


def test_truncate_field_keeps_head_and_tail() -> None:
    text = "HEAD" + ("X" * 5_000) + "TAIL"
    out, _ = truncate_field(text, budget_chars=1_000, label="x")
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")


def test_truncate_transcript_drops_oldest_first() -> None:
    turns = [
        Turn(turn_index=i, question="q" * 200, answer="a" * 200, trap_name="x")
        for i in range(1, 11)  # 10 turns × ~500 chars each = 5,000 chars total
    ]
    kept, dropped = truncate_transcript(turns, budget_chars=2_000)
    assert dropped > 0
    # most recent turns must survive
    assert kept[-1].turn_index == 10


def test_truncate_transcript_keeps_all_when_under_budget() -> None:
    turns = [
        Turn(turn_index=i, question="q", answer="a", trap_name="x")
        for i in range(1, 6)
    ]
    kept, dropped = truncate_transcript(turns, budget_chars=10_000)
    assert dropped == 0
    assert len(kept) == 5


# ─── model detection + budget computation ─────────────────────────────────


def test_detect_context_tokens_returns_a_number_for_known_model() -> None:
    # gpt-4o-mini has a known context size — even if the precise number drifts,
    # we expect a positive integer.
    n = detect_context_tokens("gpt-4o-mini")
    assert isinstance(n, int)
    assert n > 0


def test_detect_context_tokens_falls_back_for_unknown_model() -> None:
    n = detect_context_tokens("nonexistent/fake-model-xyz")
    assert n == 32_000  # FALLBACK_CONTEXT_TOKENS


def test_char_budget_for_is_positive_and_smaller_than_window() -> None:
    n = char_budget_for("gpt-4o-mini")
    raw_window = detect_context_tokens("gpt-4o-mini") * 4
    assert 0 < n < raw_window


# ─── Harness wiring ───────────────────────────────────────────────────────


def test_harness_default_context_budget_set() -> None:
    h = Harness(llm="anthropic/claude-sonnet-4-6", verbose=False)
    assert h.context_budget_chars > 0
    assert h.detected_context_tokens > 0


def test_harness_user_override_context_budget_tokens() -> None:
    h = Harness(
        llm="anthropic/claude-sonnet-4-6",
        verbose=False,
        context_budget_tokens=8_000,
    )
    # 8K tokens × 4 chars/token = 32K chars
    assert h.context_budget_chars == 8_000 * 4


# ─── End-to-end: context_truncated event fires for tiny budgets ──────────


@pytest.mark.asyncio
async def test_context_truncation_event_fires_for_tiny_budget(
    fake_llm, echo_agent
) -> None:
    """With a tiny budget and a verbose system prompt, trimming should fire and surface."""
    from proofagent_harness import AgentContext

    events: list[str] = []

    def on_event(e):
        events.append(e.type)

    # Tiny 2K-token budget — anything beyond a few hundred chars triggers trimming.
    harness = Harness(
        llm=fake_llm,
        turns=2,
        consensus="independent",
        verbose=False,
        context_budget_tokens=2_000,
    )

    big_knowledge = "POLICY DETAIL " * 5_000  # ~70K chars

    await harness.aevaluate(
        echo_agent,
        role="x",
        goal="y",
        knowledge=big_knowledge,
        context=AgentContext(system_prompt="X" * 50_000),
        on_event=on_event,
    )

    assert "context_truncated" in events, (
        f"expected at least one context_truncated event with tiny budget; "
        f"got events: {set(events)}"
    )
