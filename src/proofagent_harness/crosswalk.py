"""Which security control did this run just produce evidence for.

The compliance axis already joins a failed check to the regulations it implicates.
This module reads that same join in the direction a security team asks in:

    control -> behaviours it covers -> the checks that observe them -> the traps
    that plant them

Nothing here is new data. `compliance.FRAMEWORKS` holds the control catalog and
`data/control_behaviours.yaml` holds the mapping; this is the read side, plus the
one thing a mapping must never be allowed to hide — its denominator.

ON DENOMINATORS. A crosswalk that says "21 AIUC-1 controls" invites the reader to
hear "AIUC-1 covered". `STANDARD_SIZE` records how many controls each standard
actually has, so coverage can only ever be quoted as a fraction. NIST SP 800-53 is
deliberately absent from it: Rev. 5 is a catalog of roughly a thousand controls
across twenty families, so "11 of N" would be a meaningless ratio rather than an
honest one. Where a denominator would mislead, the right number is no number.
"""

from __future__ import annotations

from functools import lru_cache

from proofagent_harness.checks import load_checks, load_control_behaviours
from proofagent_harness.compliance import FRAMEWORKS

# The frameworks added as the agent-security crosswalk, in the order a reader
# should meet them: the agentic lists first, then the enterprise anchors.
SECURITY_FRAMEWORKS: tuple[str, ...] = (
    "owasp_asi",
    "owasp_threats",
    "owasp_llm",
    "aiuc_1",
    "nist_800_53",
)

# Controls in the published standard, where the standard has a definite count.
# Used to report coverage as a fraction so the catalog cannot read as complete.
STANDARD_SIZE: dict[str, int] = {
    "owasp_asi": 10,        # ASI01..ASI10
    "owasp_threats": 17,    # T1..T17 as of v1.1
    "owasp_llm": 10,        # LLM01:2025..LLM10:2025
    "aiuc_1": 51,           # 53 requirement ids, less E007 and E014 (retired)
}

# Why each omitted control is out of scope. Keyed by the published ref, not our
# internal id, because the omitted ones have no internal id by definition.
OMISSIONS: dict[str, dict[str, str]] = {
    "owasp_asi": {
        "ASI04": "Agentic supply chain — needs registry, dependency and AIBOM "
                 "evidence, none of which appears in a transcript.",
        "ASI07": "Insecure inter-agent communication — needs a second agent on "
                 "the wire.",
    },
    "owasp_threats": {
        "T4": "Resource overload — a load and quota property, not a behavioural one.",
        "T12": "Agent communication poisoning — multi-agent.",
        "T13": "Rogue agents in multi-agent systems — multi-agent.",
        "T14": "Human attacks on multi-agent systems — multi-agent.",
        "T16": "Insecure inter-agent protocol abuse — needs MCP/A2A traffic.",
        "T17": "Supply chain compromise — needs build and dependency evidence.",
    },
    "owasp_llm": {
        "LLM03:2025": "Supply chain — model and dependency provenance.",
        "LLM04:2025": "Data and model poisoning — training-time, not inference-time.",
        "LLM10:2025": "Unbounded consumption — a cost and rate property.",
    },
    "aiuc_1": {
        "A007": "IP violations — needs a corpus comparison the harness does not run.",
        "*": "The remaining requirements are process and organisational controls "
             "(third-party testing, vendor due diligence, quality management, "
             "incident plans, documentation) that an agent run cannot evidence.",
    },
    "nist_800_53": {
        "*": "The programme controls around the agent (CA-7 continuous monitoring, "
             "AU-11 retention, AU-6 review cadence) are the organisation's to "
             "evidence. Only controls an agent transcript speaks to directly are "
             "cataloged.",
    },
}


@lru_cache(maxsize=1)
def _checks_by_behaviour() -> dict[str, tuple[str, ...]]:
    """behaviour -> the check ids that observe it."""
    out: dict[str, list[str]] = {}
    for cid, c in load_checks().items():
        if c.probes:
            out.setdefault(c.probes, []).append(cid)
    return {b: tuple(v) for b, v in out.items()}


def checks_for_control(framework: str, control: str) -> tuple[str, ...]:
    """Every check whose failure is evidence about this control."""
    behaviours = (load_control_behaviours().get(framework) or {}).get(control) or []
    seen: dict[str, None] = {}
    for b in behaviours:
        for cid in _checks_by_behaviour().get(b, ()):
            seen[cid] = None
    return tuple(seen)


def controls_for_check(check: str) -> dict[str, tuple[str, ...]]:
    """Reverse: framework -> controls this one check produces evidence for."""
    probes = (load_checks().get(check).probes if check in load_checks() else None)
    if not probes:
        return {}
    out: dict[str, list[str]] = {}
    for fw, controls in load_control_behaviours().items():
        for control, behaviours in (controls or {}).items():
            if probes in (behaviours or []):
                out.setdefault(fw, []).append(control)
    return {fw: tuple(v) for fw, v in out.items()}


def coverage(framework: str) -> tuple[int, int | None]:
    """(controls cataloged, controls in the standard). None when a ratio would lie."""
    cataloged = len(FRAMEWORKS.get(framework, {}).get("controls", []))
    return cataloged, STANDARD_SIZE.get(framework)


def coverage_text(framework: str) -> str:
    """The fraction, phrased so it cannot be read as certification."""
    cataloged, total = coverage(framework)
    if total is None:
        return f"{cataloged} controls a transcript can evidence"
    return f"{cataloged} of {total} controls a transcript can evidence"


def rows(framework: str) -> list[dict[str, object]]:
    """One row per cataloged control: ref, title, behaviours, evidencing checks."""
    mapping = load_control_behaviours().get(framework) or {}
    out: list[dict[str, object]] = []
    for c in FRAMEWORKS.get(framework, {}).get("controls", []):
        out.append({
            "id": c["id"],
            "ref": c["ref"],
            "title": c["title"],
            "behaviours": list(mapping.get(c["id"]) or []),
            "checks": list(checks_for_control(framework, c["id"])),
        })
    return out
