"""Tests for the planner's multi-turn weaving (callbacks + follow-up probes)."""

from __future__ import annotations

import pytest

from proofagent_harness import Trap, TurnSpec

# ─── Schema shape ────────────────────────────────────────────────────────


def test_turnspec_has_weaving_fields_with_safe_defaults() -> None:
    """A bare TurnSpec must default to no weaving (backwards-compatible)."""
    t = TurnSpec(turn=1, trap=Trap(name="x", family="generic"))
    assert t.callback_to_turn is None
    assert t.is_follow_up is False
    assert t.intent_note == ""


def test_turnspec_accepts_callback() -> None:
    t = TurnSpec(
        turn=6,
        trap=Trap(name="x", family="generic"),
        callback_to_turn=2,
        intent_note="weaponize the turn-2 acknowledgement",
    )
    assert t.callback_to_turn == 2
    assert t.intent_note == "weaponize the turn-2 acknowledgement"


def test_turnspec_accepts_follow_up() -> None:
    t = TurnSpec(
        turn=5,
        trap=Trap(name="x", family="generic"),
        is_follow_up=True,
        intent_note="press the hedge from prior turn",
    )
    assert t.is_follow_up is True


# ─── Weaving step (LLM-driven, here exercised with a stubbed LLM) ─────────


@pytest.mark.asyncio
async def test_weave_strategy_applies_llm_annotations(fake_llm) -> None:
    """The weaver should apply valid annotations from the LLM."""
    from proofagent_harness.agents.planner import _weave_strategy

    fake_llm.canned_json = [
        {
            "weaves": [
                {"turn": 4, "is_follow_up": True, "intent_note": "press the hedge"},
                {"turn": 6, "callback_to_turn": 2, "intent_note": "weaponize precedent"},
            ]
        }
    ]

    plan = [
        TurnSpec(turn=i, trap=Trap(name=f"t{i}", family="generic"))
        for i in range(1, 9)
    ]
    state = {"llm": fake_llm, "skills": [], "role": "x", "business_case": "y", "goal": "z"}

    woven, count = await _weave_strategy(state, plan)  # type: ignore[arg-type]

    assert count == 2
    by_turn = {t.turn: t for t in woven}
    assert by_turn[4].is_follow_up is True
    assert by_turn[4].intent_note == "press the hedge"
    assert by_turn[6].callback_to_turn == 2
    assert by_turn[6].intent_note == "weaponize precedent"


@pytest.mark.asyncio
async def test_weave_never_modifies_turn_1_or_2(fake_llm) -> None:
    """Setup turns must stay intact even if the LLM tries to annotate them."""
    from proofagent_harness.agents.planner import _weave_strategy

    fake_llm.canned_json = [
        {
            "weaves": [
                {"turn": 1, "is_follow_up": True, "intent_note": "should be ignored"},
                {"turn": 2, "callback_to_turn": 1, "intent_note": "also ignored"},
            ]
        }
    ]
    plan = [
        TurnSpec(turn=i, trap=Trap(name=f"t{i}", family="generic"))
        for i in range(1, 6)
    ]
    state = {"llm": fake_llm, "skills": [], "role": "x", "business_case": "y", "goal": "z"}

    woven, count = await _weave_strategy(state, plan)  # type: ignore[arg-type]

    assert count == 0
    assert woven[0].is_follow_up is False
    assert woven[1].callback_to_turn is None


@pytest.mark.asyncio
async def test_weave_rejects_invalid_callback_targets(fake_llm) -> None:
    """callback_to_turn must point to a strictly earlier turn."""
    from proofagent_harness.agents.planner import _weave_strategy

    fake_llm.canned_json = [
        {
            "weaves": [
                {"turn": 5, "callback_to_turn": 5, "intent_note": "self-ref"},   # invalid
                {"turn": 5, "callback_to_turn": 9, "intent_note": "future-ref"}, # invalid (later turn)
                {"turn": 5, "callback_to_turn": 0, "intent_note": "zero"},       # invalid
            ]
        }
    ]
    plan = [
        TurnSpec(turn=i, trap=Trap(name=f"t{i}", family="generic"))
        for i in range(1, 6)
    ]
    state = {"llm": fake_llm, "skills": [], "role": "x", "business_case": "y", "goal": "z"}

    woven, _ = await _weave_strategy(state, plan)  # type: ignore[arg-type]
    assert woven[4].callback_to_turn is None  # turn-5 stayed clean


@pytest.mark.asyncio
async def test_weave_skips_when_plan_too_short(fake_llm) -> None:
    """Plans of fewer than 4 turns shouldn't be woven (callbacks need history)."""
    from proofagent_harness.agents.planner import _weave_strategy

    plan = [
        TurnSpec(turn=i, trap=Trap(name=f"t{i}", family="generic"))
        for i in range(1, 4)  # only 3 turns
    ]
    state = {"llm": fake_llm, "skills": [], "role": "x", "business_case": "y", "goal": "z"}

    woven, count = await _weave_strategy(state, plan)  # type: ignore[arg-type]
    assert count == 0
    # plan returned unchanged
    assert all(t.is_follow_up is False for t in woven)


@pytest.mark.asyncio
async def test_weave_skips_when_no_llm_available() -> None:
    """No LLM → no weaving (graceful degradation)."""
    from proofagent_harness.agents.planner import _weave_strategy

    plan = [
        TurnSpec(turn=i, trap=Trap(name=f"t{i}", family="generic"))
        for i in range(1, 9)
    ]
    state = {"llm": None, "skills": [], "role": "x", "business_case": "y", "goal": "z"}

    woven, count = await _weave_strategy(state, plan)  # type: ignore[arg-type]
    assert count == 0


# ─── Session-wide no-duplicate rule + follow-up trap inheritance ──────────


def test_dedupe_preserving_order_drops_repeats() -> None:
    from proofagent_harness.agents.planner import _dedupe_preserving_order

    a = Trap(name="alpha", family="x")
    b = Trap(name="beta", family="x")
    c = Trap(name="alpha", family="x")  # duplicate name
    out = _dedupe_preserving_order([a, b, c])
    assert [t.name for t in out] == ["alpha", "beta"]


def test_dedupe_preserving_order_keeps_first_occurrence() -> None:
    from proofagent_harness.agents.planner import _dedupe_preserving_order

    first = Trap(name="alpha", family="prompt_injection")
    later = Trap(name="alpha", family="other_family")  # same name, different obj
    out = _dedupe_preserving_order([first, later])
    assert len(out) == 1
    assert out[0] is first  # first occurrence wins


def test_follow_up_inherits_prior_trap() -> None:
    """A follow-up turn must adopt the immediately prior turn's trap."""
    from proofagent_harness.agents.planner import _inherit_traps_for_follow_ups

    real_trap = Trap(name="real_trap", family="prompt_injection")
    placeholder = Trap(name="placeholder", family="other")

    turns = [
        TurnSpec(turn=1, trap=real_trap),
        TurnSpec(turn=2, trap=placeholder, is_follow_up=True),
    ]
    out = _inherit_traps_for_follow_ups(turns)

    assert out[0].trap.name == "real_trap"
    assert out[1].trap.name == "real_trap"  # inherited from turn 1
    assert out[1].is_follow_up is True       # flag preserved


def test_follow_up_inheritance_is_chained() -> None:
    """Two consecutive follow-ups both end up on the original trap."""
    from proofagent_harness.agents.planner import _inherit_traps_for_follow_ups

    real_trap = Trap(name="anchor_trap", family="prompt_injection")
    turns = [
        TurnSpec(turn=1, trap=real_trap),
        TurnSpec(turn=2, trap=Trap(name="x", family="y"), is_follow_up=True),
        TurnSpec(turn=3, trap=Trap(name="z", family="y"), is_follow_up=True),
    ]
    out = _inherit_traps_for_follow_ups(turns)
    assert out[1].trap.name == "anchor_trap"
    assert out[2].trap.name == "anchor_trap"  # inherits from turn 2 which inherited from turn 1


def test_no_duplicate_traps_in_a_full_plan() -> None:
    """End-to-end: a plan should have unique trap names except where follow-ups intentionally re-use."""
    from proofagent_harness.agents.planner import (
        _dedupe_preserving_order,
        _inherit_traps_for_follow_ups,
    )
    from proofagent_harness.loaders import load_traps

    pool = load_traps()
    deduped = _dedupe_preserving_order(pool)

    # No duplicates in the deduped pool itself
    assert len(deduped) == len({t.name for t in deduped})

    # Build turns from this and apply follow-up inheritance
    turns = [
        TurnSpec(turn=i + 1, trap=t)
        for i, t in enumerate(deduped[:8])
    ]
    turns[3].is_follow_up = True  # turn 4 follows up turn 3
    turns[6].is_follow_up = True  # turn 7 follows up turn 6
    out = _inherit_traps_for_follow_ups(turns)

    # Count trap usage
    from collections import Counter
    counts = Counter(t.trap.name for t in out)

    # Every duplicate must be a follow-up turn
    for name, count in counts.items():
        if count > 1:
            indices = [i for i, t in enumerate(out) if t.trap.name == name]
            # All non-first occurrences must be follow-ups
            for idx in indices[1:]:
                assert out[idx].is_follow_up, (
                    f"trap {name} appears at turn {idx + 1} without is_follow_up flag"
                )
