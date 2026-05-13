"""Tests for reproducibility levers — seed pass-through + per-role temperatures."""

from __future__ import annotations

import pytest

from proofagent_harness import LLM, Harness

# ─── seed pass-through ───────────────────────────────────────────────────


def test_llm_seed_defaults_to_none() -> None:
    """An LLM with no seed argument should not pass `seed` to providers."""
    llm = LLM(model="anthropic/claude-sonnet-4-6")
    assert llm.seed is None


def test_llm_accepts_seed() -> None:
    """seed should round-trip through the LLM dataclass."""
    llm = LLM(model="openai/gpt-4.1-mini", seed=42)
    assert llm.seed == 42


def test_harness_propagates_seed_to_string_llm(fake_llm) -> None:
    """When llm is given as a string and seed is set, the constructed LLM gets it."""
    h = Harness(llm="openai/gpt-4.1-mini", seed=42, verbose=False)
    assert h.llm.seed == 42


def test_harness_propagates_seed_to_existing_llm() -> None:
    """When user passes an LLM instance with no seed, Harness fills it in."""
    user_llm = LLM(model="openai/gpt-4.1-mini")
    assert user_llm.seed is None
    h = Harness(llm=user_llm, seed=42, verbose=False)
    assert h.llm.seed == 42


def test_user_llm_seed_wins_over_harness_seed() -> None:
    """If the user explicitly set a seed on their LLM, Harness should not overwrite it."""
    user_llm = LLM(model="openai/gpt-4.1-mini", seed=999)
    h = Harness(llm=user_llm, seed=42, verbose=False)
    assert h.llm.seed == 999


def test_litellm_drops_unsupported_params() -> None:
    """`seed` must be passable for any provider — LiteLLM should silently drop
    it for providers that don't support it (e.g. Anthropic), not raise.

    This regression test guards against the LLMNotConfiguredError users hit
    when LiteLLM's `drop_params` default flips to False between releases.
    """
    import litellm
    assert litellm.drop_params is True, (
        "litellm.drop_params must be True so seed= can be passed to any "
        "provider safely. Set it in proofagent_harness.llm at import time."
    )


# ─── per-role temperature defaults ────────────────────────────────────────


@pytest.mark.asyncio
async def test_jurors_use_temperature_zero(fake_llm, echo_agent) -> None:
    """Juror calls must run at temperature=0 for deterministic scoring."""
    harness = Harness(llm=fake_llm, turns=1, consensus="independent", verbose=False)
    await harness.aevaluate(echo_agent, role="x", goal="y")

    # Juror calls go through complete_json. Each juror call should have
    # temperature=0 so the same transcript yields the same score.
    juror_temps = [
        t for t in fake_llm.last_temperature_per_call if t is not None
    ]
    # At least one call must be at 0.0 (the juror calls)
    assert 0.0 in juror_temps, (
        f"expected at least one juror call at temperature=0.0; "
        f"got temperatures: {fake_llm.last_temperature_per_call}"
    )
