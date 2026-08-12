"""Guards on the agent-security crosswalk.

A control mapping is a compliance claim wearing the costume of a data file. The
failure mode is not a crash, it is a plausible-looking id that no published
document contains, surviving review because it reads right. These tests make that
specific failure loud.

Every id asserted here was read from the primary document. If one of these fails,
the fix is to re-read the standard, not to update the expectation.
"""

from __future__ import annotations

import re

import pytest

from proofagent_harness.checks import load_checks, load_control_behaviours
from proofagent_harness.compliance import DEFAULT_FRAMEWORKS, FRAMEWORKS
from proofagent_harness.crosswalk import (
    OMISSIONS,
    SECURITY_FRAMEWORKS,
    STANDARD_SIZE,
    checks_for_control,
    controls_for_check,
    coverage,
    coverage_text,
    rows,
)

# The published identifier shape for each framework. Catches the near-misses:
# ASI1 for ASI01, LLM06 without the :2025 suffix, AC-6.9 for AC-6(9).
REF_SHAPE = {
    "owasp_asi": re.compile(r"^ASI(0[1-9]|10)$"),
    "owasp_threats": re.compile(r"^T([1-9]|1[0-7])$"),
    "owasp_llm": re.compile(r"^LLM(0[1-9]|10):2025$"),
    "aiuc_1": re.compile(r"^[A-F]\d{3}$"),
    "nist_800_53": re.compile(r"^[A-Z]{2}-\d+(\(\d+\))?$"),
}


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_every_ref_matches_the_published_identifier_shape(fw: str) -> None:
    shape = REF_SHAPE[fw]
    for c in FRAMEWORKS[fw]["controls"]:
        assert shape.match(c["ref"]), f"{fw}: {c['ref']!r} is not a published id shape"


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_refs_are_unique_within_a_framework(fw: str) -> None:
    refs = [c["ref"] for c in FRAMEWORKS[fw]["controls"]]
    assert len(refs) == len(set(refs)), f"{fw} lists a control twice"


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_omitted_controls_are_actually_absent(fw: str) -> None:
    """The documented gap has to be a real gap.

    Without this, someone adds ASI04 later, the omission note still says it is out
    of scope, and the file now argues with itself in two directions at once.
    """
    refs = {c["ref"] for c in FRAMEWORKS[fw]["controls"]}
    for omitted in OMISSIONS.get(fw, {}):
        if omitted == "*":
            continue
        assert omitted not in refs, (
            f"{fw}: {omitted} is cataloged but still listed as an omission"
        )


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_coverage_never_exceeds_the_standard(fw: str) -> None:
    cataloged, total = coverage(fw)
    assert cataloged > 0
    if total is not None:
        assert cataloged <= total, f"{fw} claims more controls than the standard has"


def test_no_denominator_is_quoted_for_800_53() -> None:
    """Rev. 5 is a catalog of ~1000 controls. '11 of 1000' is noise, not honesty."""
    assert "nist_800_53" not in STANDARD_SIZE
    assert " of " not in coverage_text("nist_800_53")


def test_coverage_text_reads_as_evidence_not_certification() -> None:
    for fw in SECURITY_FRAMEWORKS:
        assert "a transcript can evidence" in coverage_text(fw)


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_every_cataloged_control_has_a_check_that_can_fire(fw: str) -> None:
    """Mapping to a behaviour is not enough; some check must actually observe it."""
    for r in rows(fw):
        assert r["checks"], f"{fw}.{r['ref']} maps to no observable check"


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_mapped_behaviours_are_ones_a_negative_check_probes(fw: str) -> None:
    probed = {c.probes for c in load_checks().values() if c.probes}
    for control, behaviours in load_control_behaviours()[fw].items():
        for b in behaviours:
            assert b in probed, f"{fw}.{control} -> {b} is probed by no check"


def test_the_owasp_threat_taxonomy_is_the_v11_range() -> None:
    """v1.1 added T16 and T17. A mapping that stops at T15 is citing v1.0."""
    nums = sorted(int(c["ref"][1:]) for c in FRAMEWORKS["owasp_threats"]["controls"])
    assert max(nums) <= 17
    assert {"T16", "T17"} <= set(OMISSIONS["owasp_threats"]), (
        "T16/T17 must be cataloged or explicitly omitted — silence reads as a "
        "v1.0 mapping, which is what nearly every published crosswalk still is"
    )


def test_asi_and_threat_taxonomy_stay_separate() -> None:
    """Two publications, two numbering schemes. Merging them corrupts both."""
    asi = {c["ref"] for c in FRAMEWORKS["owasp_asi"]["controls"]}
    threats = {c["ref"] for c in FRAMEWORKS["owasp_threats"]["controls"]}
    assert not (asi & threats)


def test_reverse_lookup_agrees_with_forward_lookup() -> None:
    for fw in SECURITY_FRAMEWORKS:
        for r in rows(fw):
            for cid in r["checks"]:
                assert r["id"] in controls_for_check(cid).get(fw, ()), (
                    f"{fw}.{r['ref']} lists {cid} but {cid} does not list it back"
                )


def test_positive_checks_evidence_no_violation() -> None:
    """A check that earns credit must never be cited as proof of a breach."""
    for cid, c in load_checks().items():
        if c.polarity == "positive":
            assert not controls_for_check(cid), f"{cid} is positive but maps to controls"


def test_unknown_check_is_empty_not_an_error() -> None:
    assert controls_for_check("no_such_check") == {}


def test_crosswalk_frameworks_are_opt_in() -> None:
    """Adding these must not silently widen every user's default compliance call."""
    for fw in SECURITY_FRAMEWORKS:
        assert fw not in DEFAULT_FRAMEWORKS


def test_markdown_keeps_one_control_per_line() -> None:
    """Rich wraps to terminal width, and a wrapped row is no longer a table."""
    from typer.testing import CliRunner

    from proofagent_harness.cli import app

    out = CliRunner().invoke(app, ["crosswalk", "-f", "owasp_llm", "--markdown"])
    assert out.exit_code == 0
    body = [ln for ln in out.output.splitlines() if ln.startswith("| `")]
    assert len(body) == len(rows("owasp_llm"))
    for line in body:
        assert line.endswith("|"), f"row was wrapped: {line!r}"


def test_unknown_framework_fails_loudly() -> None:
    from typer.testing import CliRunner

    from proofagent_harness.cli import app

    out = CliRunner().invoke(app, ["crosswalk", "-f", "not_a_framework"])
    assert out.exit_code == 1


@pytest.mark.parametrize("fw", SECURITY_FRAMEWORKS)
def test_control_ids_are_stable_slugs(fw: str) -> None:
    """Ids are keys in stored reports; a rename orphans historical evidence."""
    for c in FRAMEWORKS[fw]["controls"]:
        assert re.match(r"^[a-z0-9_]+$", c["id"]), f"{fw}: {c['id']!r} is not a slug"
        assert checks_for_control(fw, c["id"]), f"{fw}.{c['id']} resolves to nothing"
