"""Loader tests — verify bundled assets parse and the auto-discovery works."""

from __future__ import annotations

from pathlib import Path

from proofagent_harness import CANONICAL_METRICS
from proofagent_harness.loaders import (
    TrapIndex,
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


def test_bundled_personas_load() -> None:
    personas = load_personas()
    names = {p.name for p in personas}
    assert names == {"rigorous", "lenient", "contrarian"}


def test_bundled_traps_load() -> None:
    traps = load_traps()
    assert len(traps) >= 30, f"expected >=30 traps, got {len(traps)}"
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
