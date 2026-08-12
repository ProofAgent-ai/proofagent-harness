"""A context proof must be a span of the CUSTOMER'S context, or nothing at all.

WHAT WENT WRONG. The assessor prompt demanded `"proof": "exact quote from the system prompt / tool
schema"` for every finding, and separately injected a `# GOVERNANCE` block holding `risk_tier`,
`use_case`, `frameworks_in_scope` and `obligations` so the model could grade against the right bar.
Two problems compounded:

  * A guardrail GAP has no passage to quote — the control is absent. The field was mandatory, so the
    model had to produce something.
  * The nearest quotable text was our own governance block.

Measured on a real run: 2 of 6 context proofs were `risk_tier: High risk` and
`obligations: Risk management system and conformity assessment required...`. Neither is a line the
customer wrote, so "proof" pointed at text they could not find in their files and could not edit.

The rule now: quote the context or leave it empty. An empty proof on an absence finding is the
honest answer, and it matches the record's own vocabulary, where NOT_TESTED is not a pass and
`PROOF_KIND` already admits NONE.
"""

from __future__ import annotations

import re

from proofagent_harness import context_engineering as ce

# The governance block's own field names. None may be offered as evidence about the context.
INJECTED_FIELDS = ("risk_tier", "use_case", "frameworks_in_scope", "obligations")


def _prompt_source() -> str:
    """The assessor prompt template plus the governance block builder."""
    import inspect
    return inspect.getsource(ce)


def test_the_prompt_states_the_proof_rules() -> None:
    src = _prompt_source()
    assert "# PROOF RULES" in src
    assert "CANNOT QUOTE AN ABSENCE" in src.upper()


def test_the_prompt_forbids_quoting_the_injected_governance_block() -> None:
    """The specific regression: our metadata must not be offered back as the customer's evidence."""
    src = _prompt_source()
    rules = src[src.index("# PROOF RULES"):]
    assert "GOVERNANCE" in rules, "the proof rules must name the block they exclude"
    for field in INJECTED_FIELDS:
        assert field in rules, f"the proof rules should name `{field}` as non-quotable"


def test_an_absence_finding_is_told_to_leave_proof_empty() -> None:
    src = _prompt_source()
    rules = src[src.index("# PROOF RULES"):]
    assert '"proof": ""' in rules


def test_the_governance_block_no_longer_asks_for_a_citation_it_cannot_have() -> None:
    """Two instructions used to disagree: the guardrail hint said "cite the missing control" while
    a missing control is exactly what cannot be cited."""
    src = _prompt_source()
    assert "cite the missing control specifically" not in src
    assert "NAME the missing control in `problem`" in src


def test_the_governance_block_still_supplies_the_bar_to_grade_against() -> None:
    """The fix must not throw away the risk context — grading guardrails without knowing the tier
    was the reason the block exists."""
    src = _prompt_source()
    for field in INJECTED_FIELDS:
        assert f"{field}:" in src, f"the governance block should still pass `{field}` to the model"


# ── the property, checkable against a real run ───────────────────────────────


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def test_every_nonempty_proof_is_a_literal_span_of_the_supplied_context() -> None:
    """The invariant a reader depends on: if a proof is shown, it is findable in their own files.

    Skipped when the developer-local fleet fixture is absent — this asserts against a REAL
    assessor output rather than a synthetic one, because the failure mode was the model choosing
    the wrong source, which a hand-written fixture cannot reproduce.
    """
    import json
    import pathlib

    report = pathlib.Path("/tmp/fleet_out/proof_check.json")
    ctx = pathlib.Path("/Users/fouad/Documents/GitHub/PAI/local_fleet/support_concierge")
    if not (report.exists() and ctx.is_dir()):  # pragma: no cover - fixture is developer-local
        return

    corpus = ""
    for sub in ("context", "domain_knowledge"):
        d = ctx / sub
        if d.is_dir():
            corpus += "\n".join(p.read_text() for p in d.iterdir() if p.is_file())
    haystack = _norm(corpus)

    findings = (json.loads(report.read_text()).get("context_engineering") or {}).get("findings") or []
    if not findings:  # pragma: no cover
        return

    stray = [
        (f.get("criterion"), f.get("proof"))
        for f in findings
        if (f.get("proof") or "").strip() and _norm(f["proof"]) not in haystack
    ]
    # ZERO, measured. The run this reads was produced AFTER the PROOF RULES change and returned
    # 6 findings: 4 real spans and 2 honest absences (empty proof) on the guardrail gaps that
    # previously fabricated `risk_tier: High risk`. Before the change the same agent produced 2
    # unplaceable quotes out of 6.
    assert not stray, (
        "context proofs that are not spans of the supplied context: "
        + "; ".join(f"{c}: {p!r}" for c, p in stray))


# ── the resolver: the file is found, not asserted ────────────────────────────


def _fleet_context():
    """The developer-local ten-agent fleet: a real multi-file context (prompt + tools + a
    two-file knowledge corpus), which is the case a single `source_file` cannot describe."""
    import pathlib

    from proofagent_harness.schemas import AgentContext

    root = pathlib.Path("/Users/fouad/Documents/GitHub/PAI/local_fleet/support_concierge")
    if not (root / "context").is_dir():  # pragma: no cover - fixture is developer-local
        return None
    ctx = AgentContext.from_dir(str(root / "context"))
    ctx.knowledge = str(root / "domain_knowledge")
    return ctx


def test_every_supplied_file_is_visible_to_the_assessment_by_name() -> None:
    """A user can hand over ten files. Each must be nameable, or a proof cannot cite one."""
    ctx = _fleet_context()
    if ctx is None:
        return
    names = set(ce._named_sources(ctx))
    assert {"system_prompt.md", "tools.json"} <= names
    # The corpus file by file — a directory collapsed to one entry puts the reader back to
    # "somewhere in domain_knowledge/".
    assert {"returns_policy.md", "data_handling.md"} <= names, sorted(names)


def test_a_quote_is_resolved_to_the_file_that_actually_contains_it() -> None:
    """The regression, on real files. A tool-schema quote must resolve to tools.json — it was
    reported as system_prompt.md when one path was stamped on every finding."""
    ctx = _fleet_context()
    if ctx is None:
        return
    src = ce._named_sources(ctx)
    assert ce._resolve_proof_file(
        '"description": "Retrieve an order for the VERIFIED customer only."', src) == "tools.json"
    assert ce._resolve_proof_file(
        "You resolve customer issues against the Returns and Warranty Policy", src) == "system_prompt.md"
    # The knowledge corpus is quotable too, and resolves to its own file.
    assert ce._resolve_proof_file("Refunds only to the original payment method", src) == "returns_policy.md"


def test_resolution_survives_rewrapped_whitespace() -> None:
    """A model re-wraps a passage it copied faithfully. Matching must not care."""
    ctx = _fleet_context()
    if ctx is None:
        return
    src = ce._named_sources(ctx)
    assert ce._resolve_proof_file(
        "You  resolve\n   customer issues   against the Returns", src) == "system_prompt.md"


def test_a_quote_from_our_own_injected_block_resolves_to_nothing() -> None:
    """The fabrication case, caught deterministically rather than by asking the model nicely.
    `risk_tier: High risk` comes from the harness's GOVERNANCE block, so no supplied file contains
    it and it must stay unattributed."""
    ctx = _fleet_context()
    if ctx is None:
        return
    src = ce._named_sources(ctx)
    assert ce._resolve_proof_file("risk_tier: High risk", src) == ""
    assert ce._resolve_proof_file(
        "obligations: Risk management system and conformity assessment required.", src) == ""


def test_an_empty_proof_resolves_to_nothing_rather_than_to_the_first_file() -> None:
    ctx = _fleet_context()
    if ctx is None:
        return
    src = ce._named_sources(ctx)
    assert ce._resolve_proof_file("", src) == ""
    assert ce._resolve_proof_file("   ", src) == ""
