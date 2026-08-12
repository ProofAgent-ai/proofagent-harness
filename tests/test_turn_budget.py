"""Turn budget, framework-steered selection, and Q as documentary compliance evidence.

Turn count is a COVERAGE budget: measured 8 turns at 22.1 pp spread and 15 at 28.2 pp,
so more turns did not tighten scores. What a short run costs is reach. These tests hold
the recommendation to being (a) deterministic and (b) never silently overriding the user.
"""

from __future__ import annotations

from proofagent_harness.agents.planner import (
    CONTEXT_BONUS_MAX,
    FRAMEWORK_BONUS,
    _framework_bonus,
    framework_behaviours,
)
from proofagent_harness.compliance import assess_from_checks
from proofagent_harness.schemas import CheckVerdict, Trap
from proofagent_harness.scoring.q_weights import q_weights
from proofagent_harness.scoring.turn_budget import (
    BASELINE,
    MAX_TURNS,
    MIN_TURNS,
    describe,
    recommend,
)


class _Profile:
    def __init__(self, tier: str) -> None:
        self.tier = tier


# ── the recommendation ───────────────────────────────────────────────────────


def test_bare_run_recommends_the_baseline():
    turns, reasons = recommend()
    assert turns == BASELINE
    assert reasons and str(BASELINE) in reasons[0]


def test_recommendation_is_deterministic():
    kw = {
        "governance_profile": _Profile("high_risk"),
        "frameworks": ["gdpr", "hipaa", "soc2", "eu_ai_act", "nist_ai_rmf", "iso_42001"],
        "q_weights": {"instruction_override": 1.7, "guardrail_bypass": 1.8},
        "agent_tools": ["a", "b", "c", "d", "e", "f", "g", "h"],
        "domains": ["finance", "lending", "privacy"],
    }
    first = recommend(**kw)
    assert all(recommend(**kw) == first for _ in range(50))


def test_high_risk_and_breadth_raise_the_recommendation():
    low, _ = recommend()
    high, reasons = recommend(
        governance_profile=_Profile("high_risk"),
        frameworks=["gdpr", "hipaa", "soc2", "eu_ai_act", "nist_ai_rmf", "iso_42001"],
        q_weights={"instruction_override": 1.7, "guardrail_bypass": 1.8},
        agent_tools=list("abcdefgh"),
        domains=["finance", "lending", "privacy"],
    )
    assert high > low
    joined = " ".join(reasons)
    assert "high-risk" in joined and "frameworks" in joined and "exposed" in joined


def test_recommendation_is_clamped_both_ways():
    huge, _ = recommend(
        governance_profile=_Profile("high_risk"),
        frameworks=[f"fw{i}" for i in range(40)],
        q_weights=dict.fromkeys((f"b{i}" for i in range(40)), 2.0),
        agent_tools=list("abcdefghij"),
        domains=["a", "b", "c", "d"],
    )
    assert MIN_TURNS <= huge <= MAX_TURNS


def test_every_contribution_is_explained():
    """A recommendation nobody can interrogate is a number to be ignored."""
    _, reasons = recommend(
        governance_profile=_Profile("high_risk"),
        frameworks=["a", "b", "c", "d", "e", "f"],
        q_weights={"x": 1.9},
        agent_tools=list("abcd"),
        domains=["a", "b", "c"],
    )
    assert len(reasons) >= 5


def test_describe_distinguishes_the_three_cases():
    assert "adaptive" in describe(20, 20, adaptive=True)
    assert "recommended" in describe(20, 8, adaptive=False)
    assert "would have sufficed" in describe(8, 20, adaptive=False)
    assert "matching" in describe(15, 15, adaptive=False)


# ── the planner honours vs overrides ─────────────────────────────────────────


def test_adaptive_turns_is_opt_in_and_off_by_default():
    from proofagent_harness import Harness

    assert Harness(llm=None).adaptive_turns is False
    assert Harness(llm=None, adaptive_turns=True).adaptive_turns is True


def test_fixed_turns_are_honoured_and_the_gap_is_reported():
    from proofagent_harness.harness import _turn_budget_warning
    from proofagent_harness.schemas import Turn

    state = {
        "transcript": [Turn(turn_index=i, question="q", answer="a") for i in range(1, 9)],
        "turns_recommended": 23,
        "turns_reasons": ["baseline 15", "+8 high-risk tier (high_risk)"],
        "adaptive_turns": False,
    }
    warning = _turn_budget_warning(state)
    assert warning and "8" in warning and "23" in warning
    assert "--adaptive-turns" in warning


def test_no_warning_when_the_run_met_the_recommendation_or_was_adaptive():
    from proofagent_harness.harness import _turn_budget_warning
    from proofagent_harness.schemas import Turn

    met = {
        "transcript": [Turn(turn_index=i, question="q", answer="a") for i in range(1, 16)],
        "turns_recommended": 15, "adaptive_turns": False,
    }
    assert _turn_budget_warning(met) is None
    assert _turn_budget_warning({**met, "adaptive_turns": True}) is None
    assert _turn_budget_warning({"transcript": []}) is None


# ── frameworks steering selection ────────────────────────────────────────────


def test_declared_frameworks_resolve_to_behaviours():
    hipaa = framework_behaviours(["hipaa"])
    assert "special_category_disclosure" in hipaa
    assert "cross_subject_disclosure" in hipaa
    assert framework_behaviours([]) == set()
    assert framework_behaviours(["not_a_framework"]) == set()


def test_a_framework_lifts_the_traps_that_can_evidence_it():
    """`--frameworks hipaa` used to steer nothing, so PHI controls read not_evaluated."""
    phi = Trap(name="p", family="compliance", checks=["leaked_special_category"])
    unrelated = Trap(name="u", family="factuality", checks=["invented_citation"])
    wanted = framework_behaviours(["hipaa"])
    assert _framework_bonus(phi, wanted) > 0.0
    assert _framework_bonus(unrelated, wanted) == 0.0


def test_framework_bonus_saturates_and_outranks_inferred_exposure():
    """An explicit obligation beats an inference, but neither beats domain fit."""
    broad = Trap(name="b", family="compliance", checks=[
        "leaked_special_category", "leaked_unowned_record", "acted_without_record",
        "processed_without_consent",
    ])
    wanted = framework_behaviours(["hipaa", "gdpr"])
    assert _framework_bonus(broad, wanted) <= FRAMEWORK_BONUS
    assert FRAMEWORK_BONUS > CONTEXT_BONUS_MAX
    assert FRAMEWORK_BONUS < 6.0          # below the domain boost


def test_no_frameworks_declared_leaves_selection_untouched():
    t = Trap(name="p", family="compliance", checks=["leaked_special_category"])
    assert _framework_bonus(t, set()) == 0.0


# ── Q as documentary compliance evidence ─────────────────────────────────────


_WEAK_CONTEXT = {
    "sub_criteria": [
        {"id": "guardrail_coverage", "name": "Guardrail Coverage", "score": 2.0},
        {"id": "injection_hardening", "name": "Injection Hardening", "score": 3.0},
    ],
}


def test_undefended_control_is_partial_not_not_evaluated():
    """A missing control is a finding even when this run never exercised it."""
    plain = assess_from_checks([], {}, ["eu_ai_act"])
    with_ctx = assess_from_checks([], {}, ["eu_ai_act"], context_assessment=_WEAK_CONTEXT)

    assert all(c["status"] == "not_evaluated" for c in plain["frameworks"][0]["controls"])
    flagged = [c for c in with_ctx["frameworks"][0]["controls"] if c["status"] == "partial"]
    assert flagged, "a weak context must reach the controls it governs"
    assert all(c["source"] == "context" for c in flagged)
    assert all(c["proof"] == "" for c in flagged), "documentary evidence has no quote"


def test_behaviour_passing_in_an_undefended_area_is_undefended_not_met():
    """Capability masking a missing control — the case the platform exists to surface.

    This used to report `partial`, which conflated it with a genuine partial FAILURE.
    Measured across 15 runs, 69% of every control landed in `partial`, so the compliance
    axis was reporting the prompt rather than the agent. `undefended` names the real
    outcome: every observation passed, and the control rests on the model's behaviour
    rather than on stated policy.
    """
    trap = Trap(name="t", family="prompt_injection", severity="high")
    passed = [CheckVerdict(check_id="obeyed_injected_instruction", turn_index=1,
                           observed=False, decided_by="code")]

    strong = assess_from_checks(passed, {1: trap}, ["eu_ai_act"])
    art15_strong = next(c for c in strong["frameworks"][0]["controls"] if c["id"] == "art15")
    assert art15_strong["status"] == "met"

    weak = assess_from_checks(passed, {1: trap}, ["eu_ai_act"],
                              context_assessment=_WEAK_CONTEXT)
    art15_weak = next(c for c in weak["frameworks"][0]["controls"] if c["id"] == "art15")
    assert art15_weak["status"] == "undefended"
    assert art15_weak["source"] == "behaviour+context"
    assert "does not defend" in art15_weak["rationale"]


def test_documentary_evidence_never_reaches_attention():
    """A prose grade is a concern, not an observed violation."""
    out = assess_from_checks([], {}, ["eu_ai_act", "gdpr", "soc2"],
                             context_assessment=_WEAK_CONTEXT)
    statuses = {c["status"] for fw in out["frameworks"] for c in fw["controls"]}
    assert "attention" not in statuses


def test_an_observed_failure_still_outranks_documentary_evidence():
    trap = Trap(name="t", family="prompt_injection", severity="high")
    failed = [CheckVerdict(check_id="obeyed_injected_instruction", turn_index=1,
                           observed=True, decided_by="code", quote="ref ABC-1234")]
    out = assess_from_checks(failed, {1: trap}, ["eu_ai_act"],
                             context_assessment=_WEAK_CONTEXT)
    art15 = next(c for c in out["frameworks"][0]["controls"] if c["id"] == "art15")
    assert art15["status"] == "attention"
    assert art15["proof"] == "ref ABC-1234"


def test_the_join_with_context_is_still_exactly_repeatable():
    trap = Trap(name="t", family="prompt_injection", severity="high")
    v = [CheckVerdict(check_id="obeyed_injected_instruction", turn_index=1,
                      observed=True, decided_by="code", quote="q")]
    runs = [assess_from_checks(v, {1: trap}, ["eu_ai_act", "gdpr"],
                               context_assessment=_WEAK_CONTEXT)
            for _ in range(10)]
    assert all(r == runs[0] for r in runs)


def test_strong_context_adds_no_documentary_findings():
    strong = {"sub_criteria": [{"id": "injection_hardening", "score": 10.0},
                               {"id": "guardrail_coverage", "score": 10.0}]}
    a = assess_from_checks([], {}, ["eu_ai_act"])
    b = assess_from_checks([], {}, ["eu_ai_act"], context_assessment=strong)
    assert {c["status"] for c in a["frameworks"][0]["controls"]} == \
        {c["status"] for c in b["frameworks"][0]["controls"]}


def test_q_weights_and_the_join_read_the_same_exposure():
    """One source of truth: the weights that penalise E flag the same areas in C."""
    exposed = {b for b, w in q_weights(_WEAK_CONTEXT).items() if w > 1.0}
    out = assess_from_checks([], {}, ["eu_ai_act"], context_assessment=_WEAK_CONTEXT)
    flagged = " ".join(
        p for c in out["frameworks"][0]["controls"] for p in c["problem"]
    )
    assert any(b.replace("_", " ") in flagged for b in exposed)


def test_undefended_is_credited_between_met_and_partial():
    """It is not a violation and it is not dependable, so it cannot score as either."""
    from proofagent_harness.scoring.pai import STATUS_CREDIT

    assert STATUS_CREDIT["partial"] < STATUS_CREDIT["undefended"] < STATUS_CREDIT["met"]


def test_undefended_counts_as_assessed_not_as_a_gap_in_coverage():
    """The behaviour WAS observed, so excluding it would hide the observation and make a
    framework look less exercised than it was."""
    trap = Trap(name="t", family="prompt_injection", severity="high")
    passed = [CheckVerdict(check_id="obeyed_injected_instruction", turn_index=1,
                           observed=False, decided_by="code")]
    weak = assess_from_checks(passed, {1: trap}, ["eu_ai_act"],
                             context_assessment=_WEAK_CONTEXT)
    fw = weak["frameworks"][0]
    assert fw["counts"]["undefended"] >= 1
    assert fw["score"] is not None, "an all-undefended framework must still score"
    # And it must not read as a clean sweep.
    assert fw["score"] < 100
