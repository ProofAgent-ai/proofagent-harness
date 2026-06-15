"""Tests for the domain-aware trap selection in the planner."""

from __future__ import annotations

import math

from proofagent_harness.agents.planner import (
    MIN_COMPOSITE_TRAPS,
    MIN_DOMAIN_TRAPS,
    MIN_FACTUALITY_TRAPS,
    _difficulty,
    _is_composite,
    _is_critical,
    _is_factuality,
    _select_traps,
)
from proofagent_harness.loaders import load_traps


def test_universal_traps_always_included() -> None:
    """A universal trap (e.g., system_prompt_leak) should appear regardless of domain."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["healthcare"], n=12)
    chosen_names = {t.name for t in chosen}
    # At least one universal trap should land in a 12-turn plan.
    universal_names = {t.name for t in traps if t.universal}
    assert chosen_names & universal_names, "expected at least one universal trap in the plan"


def test_domain_specific_trap_picked_when_domain_matches() -> None:
    """HIPAA trap should appear when domain=healthcare."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["healthcare"], n=20)
    assert "hipaa_phi_exposure" in {t.name for t in chosen}


def test_domain_specific_trap_skipped_when_domain_mismatch() -> None:
    """HIPAA trap should NOT appear in a small plan for a non-healthcare agent."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["code", "engineering"], n=8)
    assert "hipaa_phi_exposure" not in {t.name for t in chosen}, \
        "HIPAA trap leaked into a code-domain plan"


def test_pci_picked_for_payments() -> None:
    """PCI trap should appear when domain=payments."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["payments", "retail"], n=10)
    assert "pci_card_data" in {t.name for t in chosen}


def test_no_domains_means_pure_universals() -> None:
    """With no domain hints, only universals + metric-coverage traps fill the plan."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=[], n=6)
    # No HIPAA / PCI / SOX (all domain-specific) in a generic 6-turn plan
    chosen_names = {t.name for t in chosen}
    assert "hipaa_phi_exposure" not in chosen_names
    assert "sox_financial_disclosure" not in chosen_names


def test_metric_coverage_preserved() -> None:
    """Even with domain filtering, every active metric must get covered."""
    traps = load_traps()
    metrics = ["safety", "hallucination_resistance", "manipulation_resistance"]
    chosen = _select_traps(traps, metrics, domains=["healthcare"], n=8)
    covered = set()
    for t in chosen:
        covered.update(t.metrics)
    for m in metrics:
        assert m in covered, f"metric {m} not covered by selection"


def test_protected_class_bias_picked_for_hr() -> None:
    """The bias trap should fire for HR agents."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["hr"], n=10)
    assert "protected_class_bias" in {t.name for t in chosen}


def test_code_safety_picked_for_code_agents() -> None:
    """Code-safety traps should fire for code-domain agents."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["code", "engineering"], n=10)
    chosen_names = {t.name for t in chosen}
    assert chosen_names & {"malicious_code_generation", "insecure_code_recommendation"}


# ─── 30% prompt-injection / hallucination floor ─────────────────────────


def test_critical_share_floor_in_8_turn_plan() -> None:
    """An 8-turn plan must include >=30% prompt_injection / hallucination traps."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety", "task_success"], domains=["healthcare"], n=8)
    n_critical = sum(1 for t in chosen if _is_critical(t))
    assert n_critical >= math.ceil(8 * 0.30), (
        f"expected >=3 critical traps (prompt_injection + hallucination_resistance), "
        f"got {n_critical}: {[t.name for t in chosen if _is_critical(t)]}"
    )


def test_critical_share_floor_in_4_turn_plan() -> None:
    """A 4-turn plan must include >=2 prompt_injection / hallucination traps."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["retail"], n=4)
    n_critical = sum(1 for t in chosen if _is_critical(t))
    assert n_critical >= math.ceil(4 * 0.30)  # ≥ 2


def test_critical_share_present_even_with_no_domain_match() -> None:
    """No domain hints + 6 turns should still meet the 30% floor."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=[], n=6)
    n_critical = sum(1 for t in chosen if _is_critical(t))
    assert n_critical >= 2  # ceil(6 * 0.30)


def test_critical_picks_include_both_prompt_injection_and_hallucination() -> None:
    """The mix should include both flavors of critical, not just one."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety", "task_success"], domains=["support"], n=10)

    families = {t.family for t in chosen}
    metrics_covered = set()
    for t in chosen:
        metrics_covered.update(t.metrics)

    assert "prompt_injection" in families, (
        f"plan missing prompt_injection family: {[t.family for t in chosen]}"
    )
    assert "hallucination_resistance" in metrics_covered, (
        "plan missing hallucination_resistance metric coverage"
    )


def test_critical_share_with_minimum_turns() -> None:
    """Even a 1-turn plan must pick at least 1 critical trap."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["retail"], n=1)
    assert len(chosen) == 1
    assert _is_critical(chosen[0])  # the one slot must be critical


def test_is_critical_classifier() -> None:
    """The _is_critical helper should match the documented rule."""
    traps = load_traps()
    by_name = {t.name: t for t in traps}

    # prompt_injection family — always critical
    assert _is_critical(by_name["instruction_override"])
    assert _is_critical(by_name["system_prompt_leak"])

    # hallucination_resistance metric — always critical
    assert _is_critical(by_name["fabricated_citations"])
    assert _is_critical(by_name["confident_falsehood"])

    # neither — not critical
    assert not _is_critical(by_name["pricing_manipulation"])
    assert not _is_critical(by_name["protected_class_bias"])


# ─── Mandatory factuality floor ───────────────────────────────────────────


def test_factuality_floor_in_8_turn_plan() -> None:
    """An 8-turn plan must include >=MIN_FACTUALITY_TRAPS factuality traps."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety", "task_success"], domains=["healthcare"], n=8)
    n_factuality = sum(1 for t in chosen if _is_factuality(t))
    assert n_factuality >= MIN_FACTUALITY_TRAPS, (
        f"expected >={MIN_FACTUALITY_TRAPS} factuality traps, got {n_factuality}: "
        f"{[t.name for t in chosen if _is_factuality(t)]}"
    )


def test_factuality_floor_with_no_domain() -> None:
    """A generic plan with no domain hints still includes the factuality floor."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=[], n=6)
    n_factuality = sum(1 for t in chosen if _is_factuality(t))
    assert n_factuality >= MIN_FACTUALITY_TRAPS


def test_factuality_floor_in_4_turn_plan() -> None:
    """Even a small 4-turn plan must include the factuality floor."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["retail"], n=4)
    n_factuality = sum(1 for t in chosen if _is_factuality(t))
    assert n_factuality >= MIN_FACTUALITY_TRAPS


def test_factuality_floor_clamps_to_plan_size() -> None:
    """A 1-turn plan can't fit 2 factuality traps; floor clamps to n."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["retail"], n=1)
    assert len(chosen) == 1
    # The single slot may or may not be factuality, but the function must
    # still return 1 trap (no crash, no oversampling).


def test_factuality_floor_compatible_with_critical_share() -> None:
    """Both the factuality floor AND the 30% critical share must hold."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety", "task_success"], domains=["finance"], n=10)
    n_factuality = sum(1 for t in chosen if _is_factuality(t))
    n_critical = sum(1 for t in chosen if _is_critical(t))
    assert n_factuality >= MIN_FACTUALITY_TRAPS
    assert n_critical >= math.ceil(10 * 0.30)


def test_factuality_traps_distinct_in_selection() -> None:
    """The two reserved factuality slots use different traps (no duplicates)."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["support"], n=8)
    factuality_picks = [t for t in chosen if _is_factuality(t)]
    names = [t.name for t in factuality_picks]
    assert len(set(names)) == len(names), f"duplicate factuality traps: {names}"


def test_is_factuality_classifier() -> None:
    """The _is_factuality helper recognizes the factuality family."""
    traps = load_traps()
    by_name = {t.name: t for t in traps}

    # Existing factuality traps
    assert _is_factuality(by_name["fabricated_citations"])
    assert _is_factuality(by_name["confident_falsehood"])
    assert _is_factuality(by_name["stale_information"])

    # New factuality traps (this conversation's additions)
    assert _is_factuality(by_name["legal_citation_fabrication"])
    assert _is_factuality(by_name["real_person_defamation"])
    assert _is_factuality(by_name["fictitious_policy_invention"])

    # Non-factuality traps
    assert not _is_factuality(by_name["instruction_override"])
    assert not _is_factuality(by_name["pricing_manipulation"])
    assert not _is_factuality(by_name["protected_class_bias"])


# ─── v0.5.0 — sharper / more challenging selection ──────────────────────────


def test_composite_floor_in_large_plan() -> None:
    """A 20-turn plan must include >=MIN_COMPOSITE_TRAPS composite/chained attacks
    (the hardest class) — not a stack of single-shot traps."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["payments", "support"], n=20)
    n_composite = sum(1 for t in chosen if _is_composite(t))
    assert n_composite >= MIN_COMPOSITE_TRAPS, (
        f"expected >={MIN_COMPOSITE_TRAPS} composite attacks, got {n_composite}: "
        f"{[t.name for t in chosen if _is_composite(t)]}"
    )


def test_domain_matched_floor_when_domains_inferred() -> None:
    """When domains are inferred and matched traps exist, the plan guarantees a
    floor of domain-matched traps (not just a score nudge)."""
    traps = load_traps()
    domains = ["payments", "support", "finance"]
    available = sum(1 for t in traps if t.domains and (set(t.domains) & set(domains)))
    chosen = _select_traps(traps, ["safety"], domains=domains, n=20)
    matched = sum(1 for t in chosen if t.domains and (set(t.domains) & set(domains)))
    assert matched >= min(MIN_DOMAIN_TRAPS, available), (
        f"expected >={min(MIN_DOMAIN_TRAPS, available)} domain-matched traps, got {matched}"
    )


def test_family_diversity_in_large_plan() -> None:
    """A 20-turn plan should span MANY attack families (broad coverage), not
    repeat one family — the family-diverse fill in action."""
    traps = load_traps()
    chosen = _select_traps(traps, ["safety"], domains=["payments"], n=20)
    families = {t.family for t in chosen}
    assert len(families) >= 8, f"expected >=8 distinct families, got {len(families)}: {families}"


def test_selection_prefers_harder_traps() -> None:
    """The sharpened selection should be meaningfully harder than a random draw:
    its mean difficulty must beat the library's average difficulty."""
    traps = load_traps()
    lib_mean = sum(_difficulty(t) for t in traps) / len(traps)
    chosen = _select_traps(traps, ["safety"], domains=["payments", "support"], n=20)
    sel_mean = sum(_difficulty(t) for t in chosen) / len(chosen)
    assert sel_mean > lib_mean, (
        f"selection mean difficulty {sel_mean:.2f} should exceed library mean {lib_mean:.2f}"
    )


def test_is_composite_classifier() -> None:
    """_is_composite recognizes chained / compound / multi-step attacks."""
    traps = load_traps()
    by_name = {t.name: t for t in traps}
    assert _is_composite(by_name["universal_jailbreak_chain"])
    assert _is_composite(by_name["social_engineering_combined_chain"])
    assert _is_composite(by_name["gradual_escalation"])
    # a plain single-shot trap is not composite
    assert not _is_composite(by_name["pricing_manipulation"])
