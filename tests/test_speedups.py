"""Speed shortcuts: the safe ones keep results, the opt-in ones stay off by default and
are recorded in the report when on."""

from __future__ import annotations

import asyncio

from proofagent_harness import Harness
from proofagent_harness.agents import juror
from proofagent_harness.schemas import CheckVerdict, JurorScore

from .test_check_pipeline_e2e import VotingLLM, _agent


def _js(persona: str, metric: str, votes: list[tuple[str, int, bool]], score: float = 0.0) -> JurorScore:
    return JurorScore(
        persona=persona, metric=metric, score=score, reasoning="",
        check_votes=[CheckVerdict(check_id=c, turn_index=t, observed=o, decided_by="llm",
                                  quote="q" if o else "") for c, t, o in votes],
    )


def _run(**env):
    import os
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return Harness(llm=VotingLLM(), turns=3, seed=42, verbose=False,
                       personas=["rigorous", "lenient"], consensus="debate").evaluate(
            _agent, role="a credit support agent", business_case="consumer lending",
            goal="answer borrower questions without leaking data")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── #1 transcript first ──────────────────────────────────────────────────────

def test_split_assignment_moves_only_the_per_call_tail() -> None:
    sys = "CONTRACTS\nCONTEXT\n## Your persona: rigorous\nbody\n## Metric under review: safety\n"
    prefix, tail = juror._split_assignment(sys)
    assert "Your persona" not in prefix and prefix.startswith("CONTRACTS")
    assert tail.startswith("## Your persona: rigorous") and "safety" in tail
    assert juror._split_assignment("no marker here") == ("no marker here", "")


# ── #6 concurrency ───────────────────────────────────────────────────────────

def test_local_jury_defaults_to_two_slots_and_env_wins(monkeypatch) -> None:
    class L:
        model = "lm_studio/google/gemma"
        api_base = ""
    class C:
        model = "gpt-4.1"
        api_base = ""
    monkeypatch.delenv("PROOFAGENT_JURY_CONCURRENCY", raising=False)
    assert juror._jury_concurrency(L()) == 2
    assert juror._jury_concurrency(C()) == 18
    monkeypatch.setenv("PROOFAGENT_JURY_CONCURRENCY", "5")
    assert juror._jury_concurrency(L()) == 5


def test_one_cap_bounds_every_juror_call(monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_JURY_CONCURRENCY", "2")
    live = peak = 0

    async def fake(state, *a, **k):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    monkeypatch.setattr(juror, "_score_once_impl", fake)

    async def go():
        await asyncio.gather(*[juror._score_once({}, None, "m", []) for _ in range(9)])
    asyncio.run(go())
    assert peak == 2


# ── #2 / #3 / #4 debate helpers ──────────────────────────────────────────────

def test_panel_converged_on_unanimous_checks_only() -> None:
    same = [_js("a", "safety", [("x", 1, False)]), _js("b", "safety", [("x", 1, False)])]
    split = [_js("a", "safety", [("x", 1, False)]), _js("b", "safety", [("x", 1, True)])]
    assert juror._panel_converged(same, 1.0)
    assert not juror._panel_converged(split, 1.0)
    assert not juror._panel_converged(same[:1], 1.0)          # one juror is not a panel


def test_unanimous_votes_carry_quotes_and_skip_splits() -> None:
    prior = [_js("a", "safety", [("x", 1, True), ("y", 2, False)]),
             _js("b", "safety", [("x", 1, True), ("y", 2, True)])]
    carry = juror._unanimous_votes(prior)
    assert set(carry) == {("x", 1)}
    assert carry[("x", 1)].observed is True and carry[("x", 1)].quote == "q"


# ── end to end: defaults unchanged, flags recorded ───────────────────────────

def test_shortcuts_are_off_by_default_and_recorded_when_on() -> None:
    base = _run()
    assert base.metadata["speed_flags"] == []
    fast = _run(PROOFAGENT_DEBATE_EARLY_STOP="1", PROOFAGENT_DEBATE_DISPUTED_ONLY="1",
                PROOFAGENT_DEBATE_DISPUTED_CHECKS="1", PROOFAGENT_JURY_COMBINED_BALLOT="1",
                PROOFAGENT_JURY_TRANSCRIPT_FIRST="1", PROOFAGENT_CONDUCTOR_PREFETCH="1")
    assert fast.metadata["speed_flags"] == sorted([
        "PROOFAGENT_CONDUCTOR_PREFETCH", "PROOFAGENT_DEBATE_DISPUTED_CHECKS",
        "PROOFAGENT_DEBATE_DISPUTED_ONLY", "PROOFAGENT_DEBATE_EARLY_STOP",
        "PROOFAGENT_JURY_COMBINED_BALLOT", "PROOFAGENT_JURY_TRANSCRIPT_FIRST"])
    # Every metric the default run scored is still scored with all shortcuts on.
    assert set(fast.per_metric) == set(base.per_metric)
