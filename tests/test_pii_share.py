"""Every plan gives at least a quarter of its rounds to personal-data attacks (PII, exfiltration, privacy law)."""
import math

import pytest

from proofagent_harness.agents import planner as P
from proofagent_harness.loaders import load_traps


@pytest.fixture(scope="module")
def library():
    return load_traps()


def test_the_library_has_pii_traps_of_every_kind(library):
    pii = [t for t in library if P.is_pii_trap(t)]
    names = {t.name for t in pii}
    assert len(pii) >= 20
    assert {"pii_extraction", "gdpr_data_subject_request", "hipaa_phi_exposure", "pci_card_data"} <= names
    assert all(t.family == "data_exfiltration" for t in library if t.family == "data_exfiltration" and P.is_pii_trap(t))


@pytest.mark.parametrize("n", [1, 4, 10, 20, 35])
def test_a_plan_without_pii_gets_a_quarter_of_its_rounds(library, n):
    base = P._select_traps([t for t in library if not P.is_pii_trap(t)], ["safety"], [], n, seed=3)
    plan, added = P._ensure_pii_share(base, library, [], ["safety"], [], seed=3)
    assert len(plan) == len(base)
    assert sum(P.is_pii_trap(t) for t in plan) == math.ceil(len(base) * P.PII_SHARE) == added
    assert len({t.name for t in plan}) == len(plan)  # no duplicate traps


def test_pinned_traps_are_never_replaced_and_stay_first(library):
    base = P._select_traps([t for t in library if not P.is_pii_trap(t)], ["safety"], [], 8, seed=1)
    plan, _ = P._ensure_pii_share(base, library, base[:3], ["safety"], [], seed=1)
    assert [t.name for t in plan[:3]] == [t.name for t in base[:3]]


def test_a_plan_already_at_the_share_is_unchanged(library):
    pii = [t for t in library if P.is_pii_trap(t)][:3]
    other = [t for t in library if not P.is_pii_trap(t)][:5]
    plan, added = P._ensure_pii_share(pii + other, library, [], ["safety"], [], seed=1)
    assert added == 0 and plan == pii + other


def test_the_pii_rounds_are_spread_through_the_run(library):
    base = P._select_traps([t for t in library if not P.is_pii_trap(t)], ["safety"], [], 20, seed=5)
    plan, _ = P._ensure_pii_share(base, library, [], ["safety"], [], seed=5)
    idx = [i for i, t in enumerate(plan) if P.is_pii_trap(t)]
    assert idx[0] < 10 and idx[-1] >= 10  # not bunched at one end


def test_a_follow_up_never_overwrites_a_pii_round(library):
    from proofagent_harness.schemas import TurnSpec

    pii = next(t for t in library if P.is_pii_trap(t))
    other = next(t for t in library if not P.is_pii_trap(t))
    turns = [TurnSpec(turn=1, trap=other, target_behavior=""), TurnSpec(turn=2, trap=pii, target_behavior="", is_follow_up=True),
             TurnSpec(turn=3, trap=other, target_behavior="", is_follow_up=True)]
    out = P._inherit_traps_for_follow_ups(turns)
    assert out[1].trap.name == pii.name and not out[1].is_follow_up  # the PII round kept its trap
    assert out[2].trap.name == pii.name  # an ordinary follow-up still inherits


# ── real-world PII traps (traps/pii_realworld, tagged pii-realworld) ──

def test_the_realworld_pii_traps_are_in_the_library_and_tagged(library):
    rw = [t for t in library if P.is_realworld_trap(t)]
    assert len(rw) == 20
    assert all(P.is_pii_trap(t) for t in rw)  # every real-world trap is a PII trap
    assert sum(P._has_injection(t) for t in rw) >= 10
    assert {t.family for t in rw} == {"data_exfiltration", "prompt_injection"}
    assert all(t.seeds and len(t.seeds[0]) > 100 for t in rw)  # the full attack text is the one-shot seed


@pytest.mark.parametrize("n", [3, 8, 20, 35])
def test_every_plan_gets_its_realworld_share_with_an_injection(library, n):
    base = P._select_traps([t for t in library if not P.is_pii_trap(t)], ["safety"], [], n, seed=11)
    plan, _ = P._ensure_realworld_share(base, library, [], ["safety"], [], seed=11)
    rw = [t for t in plan if P.is_realworld_trap(t)]
    assert len(rw) >= math.ceil(len(base) * P.REALWORLD_SHARE)
    assert any(P._has_injection(t) for t in rw)
    assert len(plan) == len(base) and len({t.name for t in plan}) == len(plan)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_every_plan_has_a_prompt_injection_carrying_heavy_pii(library, seed):
    base = P._select_traps([t for t in library if not P.is_pii_trap(t)], ["safety"], [], 8, seed=seed)
    plan, _ = P._ensure_realworld_share(base, library, [], ["safety"], [], seed=seed)
    heavy = [t for t in plan if P.is_heavy_combo(t)]
    assert heavy and all(P._has_injection(t) and sum(x.startswith("pii-") for x in t.tags) >= 6 for t in heavy)
