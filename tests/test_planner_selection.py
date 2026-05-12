"""Tests for the domain-aware trap selection in the planner."""

from __future__ import annotations

import math

from proofagent_harness.agents.planner import _is_critical, _select_traps
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
        f"plan missing hallucination_resistance metric coverage"
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
    assert _is_critical(by_name["citation_fabrication"])
    assert _is_critical(by_name["confident_falsehood"])

    # neither — not critical
    assert not _is_critical(by_name["pricing_manipulation"])
    assert not _is_critical(by_name["protected_class_bias"])
