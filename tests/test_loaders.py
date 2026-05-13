"""Loader tests — verify bundled assets parse and the auto-discovery works."""

from __future__ import annotations

from pathlib import Path

from proofagent_harness import CANONICAL_METRICS
from proofagent_harness.loaders import (
    load_knowledge,
    load_personas,
    load_skills,
    load_trap_index,
    load_traps,
    select_traps,
)


def test_bundled_skills_load() -> None:
    skills = load_skills()
    names = {s.name for s in skills}
    assert "planning" in names
    assert "conducting" in names
    assert "reporting" in names
    for metric in CANONICAL_METRICS:
        assert f"score_{metric}" in names, f"missing scoring skill for {metric}"


def test_conducting_skill_includes_false_premise_weaving() -> None:
    """The conducting skill must tell the conductor LLM to weave subtle false
    premises into non-factuality turns. This is what makes hallucination_resistance
    a whole-transcript metric instead of just a per-trap one."""
    skills = load_skills()
    conducting = next(s for s in skills if s.name == "conducting")
    body = conducting.body.lower()

    # Core directives the skill must carry
    assert "false-premise weaving" in body or "false premise" in body
    # Skip rule for factuality-family traps (no double-up)
    assert "factuality" in body and "skip" in body
    # The 30-50% targeting rule
    assert "30" in body and "50" in body
    # At least one concrete category named
    assert any(
        cue in body
        for cue in ("misstated company", "fabricated quote", "invented section")
    )


def test_hallucination_rubric_includes_cross_turn_audit() -> None:
    """The hallucination_resistance scoring rubric must instruct the juror to
    audit EVERY user turn for embedded false premises, not just the dedicated
    factuality-family turns. Otherwise the false-premise weaving (which the
    conductor does in ~30-50% of non-factuality turns) has no scoring signal."""
    skills = load_skills()
    rubric = next(s for s in skills if s.name == "score_hallucination_resistance")
    body = rubric.body.lower()

    assert "cross-turn premise" in body or "every user turn" in body
    # All three classification outcomes must be named
    assert "corrected" in body
    assert "hedged" in body
    assert "accepted" in body
    # The whole-transcript directive (not just factuality traps)
    assert "whole transcript" in body or "across the whole" in body


def test_bundled_personas_load() -> None:
    personas = load_personas()
    names = {p.name for p in personas}
    assert names == {"rigorous", "lenient", "contrarian"}


def test_bundled_traps_load() -> None:
    traps = load_traps()
    assert len(traps) >= 35, f"expected >=35 traps, got {len(traps)}"
    families = {t.family for t in traps}
    # core families
    assert "compliance" in families
    assert "social_engineering" in families
    assert "prompt_injection" in families
    assert "policy_drift" in families
    assert "factuality" in families
    # expanded families
    assert "data_exfiltration" in families
    assert "bias" in families
    assert "business_logic" in families
    assert "code_safety" in families
    assert "tool_misuse" in families


def test_new_trap_families_have_content() -> None:
    """Verify the new families ship with at least one trap each."""
    traps = load_traps()
    by_family: dict[str, int] = {}
    for t in traps:
        by_family[t.family] = by_family.get(t.family, 0) + 1
    for family in (
        "data_exfiltration",
        "bias",
        "business_logic",
        "code_safety",
        "tool_misuse",
    ):
        assert by_family.get(family, 0) >= 1, f"{family} family has no traps"


def test_compliance_traps_present() -> None:
    traps = load_traps()
    names = {t.name for t in traps}
    assert "gdpr_data_subject_request" in names
    assert "ccpa_do_not_sell" in names
    assert "hipaa_phi_exposure" in names
    assert "pci_card_data" in names
    assert "sox_financial_disclosure" in names


def test_advanced_attack_traps_present() -> None:
    """Traps that target modern-agent weak spots (sycophancy, hypothetical
    framing, refusal-channel attacks, policy gaslighting, composition)."""
    traps = load_traps()
    names = {t.name for t in traps}
    assert "hypothetical_role_play" in names
    assert "refusal_channel_attack" in names
    assert "sycophancy_exploit" in names
    assert "policy_evolution_claim" in names
    assert "compositional_disclosure" in names


def test_documented_hallucination_traps_present() -> None:
    """Factuality traps modeled on documented production-incident patterns."""
    traps = load_traps()
    names = {t.name for t in traps}
    # Mata v. Avianca pattern
    assert "legal_citation_fabrication" in names
    # Walters / Holmen pattern
    assert "real_person_defamation" in names
    # Long-tail entity confabulation
    assert "obscure_entity_invention" in names
    # False-premise historical fabrication ("King Renoit")
    assert "historical_fact_fabrication" in names
    # IKEA Ringsted pattern
    assert "fabricated_local_business_info" in names
    # 28-29% academic fab rate
    assert "academic_citation_fabrication" in names
    # GPT-4.1 prompting guide tool-arg pattern
    assert "tool_input_hallucination" in names
    # Moffatt v. Air Canada pattern
    assert "fictitious_policy_invention" in names
    # Long-context drift
    assert "long_context_factual_drift" in names
    # Confident numerical fabrication
    assert "numerical_fabrication" in names


def test_factuality_family_has_at_least_min_floor_capacity() -> None:
    """The bundled factuality library must hold enough traps to satisfy the
    mandatory floor across multiple turn budgets."""
    from proofagent_harness.agents.planner import MIN_FACTUALITY_TRAPS

    traps = load_traps()
    factuality = [t for t in traps if t.family == "factuality"]
    assert len(factuality) >= MIN_FACTUALITY_TRAPS + 1, (
        f"need >{MIN_FACTUALITY_TRAPS} factuality traps to fill the floor "
        f"with selection diversity; got {len(factuality)}"
    )


def test_select_traps_filters_by_metric() -> None:
    traps = load_traps()
    safety_traps = select_traps(traps, metric="safety")
    assert len(safety_traps) > 0
    for t in safety_traps:
        assert "safety" in t.metrics or not t.metrics


def test_select_traps_filters_by_family() -> None:
    traps = load_traps()
    pi_traps = select_traps(traps, family="prompt_injection")
    assert all(t.family == "prompt_injection" for t in pi_traps)


def test_load_knowledge_inline_string() -> None:
    text = load_knowledge("hello world")
    assert text == "hello world"


def test_load_knowledge_dict() -> None:
    text = load_knowledge({"policy_a": "no refunds", "policy_b": "verify ID"})
    assert "policy_a" in text
    assert "no refunds" in text


def test_load_knowledge_file(tmp_path: Path) -> None:
    p = tmp_path / "kb.md"
    p.write_text("the quick brown fox")
    text = load_knowledge(str(p))
    assert "quick brown fox" in text


def test_load_knowledge_dir(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    text = load_knowledge(str(tmp_path))
    assert "alpha" in text
    assert "beta" in text


# ─── TrapIndex (inverted lookups) ────────────────────────────────────────


def test_trap_index_builds_from_traps() -> None:
    idx = load_trap_index()
    s = idx.stats()
    assert s["total"] >= 30
    assert s["universal"] >= 15  # most cross-cutting traps are universal
    assert s["domain_specific"] >= 5
    assert s["domains"] >= 5
    assert s["families"] >= 10


def test_trap_index_by_domain_lookup() -> None:
    idx = load_trap_index()
    # HIPAA trap should be findable via the healthcare domain
    healthcare = {t.name for t in idx.by_domain.get("healthcare", [])}
    assert "hipaa_phi_exposure" in healthcare


def test_trap_index_by_metric_lookup() -> None:
    idx = load_trap_index()
    safety_traps = {t.name for t in idx.by_metric.get("safety", [])}
    assert "pci_card_data" in safety_traps
    assert "instruction_override" in safety_traps


def test_trap_index_by_family_lookup() -> None:
    idx = load_trap_index()
    pi = {t.name for t in idx.by_family.get("prompt_injection", [])}
    assert "system_prompt_leak" in pi
    assert "instruction_override" in pi


def test_trap_index_relevant_pool_excludes_mismatched_domains() -> None:
    """A code-only pool should NOT include healthcare-only traps."""
    idx = load_trap_index()
    pool = {t.name for t in idx.relevant_pool(["code"])}
    assert "malicious_code_generation" in pool       # domain-match
    assert "system_prompt_leak" in pool              # universal
    assert "hipaa_phi_exposure" not in pool          # different domain


def test_trap_index_for_domains_unions_results() -> None:
    """Asking for multiple domains returns the union."""
    idx = load_trap_index()
    result = {t.name for t in idx.for_domains(["healthcare", "payments"])}
    assert "hipaa_phi_exposure" in result
    assert "pci_card_data" in result


def test_trap_index_universal_traps_are_marked() -> None:
    idx = load_trap_index()
    for t in idx.universals:
        assert t.universal is True
    for t in idx.domain_specific:
        assert t.universal is False
        assert t.domains  # by definition
