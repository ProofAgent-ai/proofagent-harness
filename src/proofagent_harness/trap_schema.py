"""Canonical trap manifest schema — single source of truth for validation.

Used by:
  - The CLI command ``proof traps validate`` to lint the bundled library
    and any user-supplied directories before they ship.
  - The frontend trap parser at ``frontend-prod/src/lib/trapParser.ts``
    (kept in sync manually — this module is the authoritative source).
  - Third-party trap packs (``proofagent-traps-*``) to self-check
    before publishing.

Design intent
-------------
The Harness loader (``loaders._parse_trap``) is intentionally permissive —
it fills missing fields with empty defaults so every trap loads cleanly.
This module **does not change loader behaviour**. It only describes the
*recommended* canonical shape so contributors get consistent feedback,
and adds the section-header aliases the field has converged on:

  * ``# Multi-turn escalation script`` is treated equivalent to
    ``# Multi-turn escalation``.
  * ``# Fail criteria (critical fail if any)`` is treated equivalent to
    ``# Fail criteria``.

Vocabularies (``CANONICAL_METRICS``, ``CANONICAL_FAMILIES``,
``CANONICAL_SEVERITIES``) are derived from the bundled library and are
considered the public contract for first-class library inclusion. Custom
trap packs may go beyond them; the validator surfaces non-canonical
values as warnings, not errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

from proofagent_harness.schemas import CANONICAL_METRICS, METRIC_ALIASES

# ---------------------------------------------------------------------------
#  Canonical vocabularies
# ---------------------------------------------------------------------------

CANONICAL_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")

CANONICAL_FAMILIES: tuple[str, ...] = (
    "bias",
    "business_logic",
    "code_safety",
    "compliance",
    "data_exfiltration",
    "factuality",
    "policy_drift",
    "prompt_injection",
    "social_engineering",
    "tool_misuse",
    "verbal_abuse",
)

#: Sections the Harness loader actively consumes via ``_extract_section``.
#: Anything else in a trap body is preserved verbatim but not parsed.
CONSUMED_SECTIONS: tuple[str, ...] = (
    "Pattern",
    "Seed examples",
    "Pass criteria",
    "Fail criteria",
)

#: Section-header aliases — the right-hand side is the canonical form.
#: When ``_extract_section`` looks for "Multi-turn escalation" it should
#: also match "Multi-turn escalation script", etc. The frontend parser
#: applies the same normalisation before bucketing rich/extra sections.
SECTION_ALIASES: dict[str, str] = {
    "multi-turn escalation script":         "multi-turn escalation",
    "fail criteria (critical fail if any)": "fail criteria",
}

#: Body sections that are encouraged for richness but never required.
RECOMMENDED_RICH_SECTIONS: tuple[str, ...] = (
    "Multi-turn escalation",
    "Core attack axes",
    "Extreme trap cases",
    "Auto-scoring signals",
    "Ideal response pattern",
    "Domain variants",
    "Recommended safe phrasing",
    "Trap-specific grading note",
)

# ---------------------------------------------------------------------------
#  Validation results
# ---------------------------------------------------------------------------


@dataclass
class TrapValidation:
    """Outcome of validating one trap file against the canonical schema."""

    path: Path
    name: str
    family: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class TrapLibraryValidation:
    """Aggregated result for an entire directory of trap manifests."""

    results: list[TrapValidation]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def error_count(self) -> int:
        return sum(len(r.errors) for r in self.results)

    @property
    def warning_count(self) -> int:
        return sum(len(r.warnings) for r in self.results)


# ---------------------------------------------------------------------------
#  Validator
# ---------------------------------------------------------------------------


def normalize_section_header(header: str) -> str:
    """Normalise a section header for matching: lower-cased, scenarios collapsed.

    >>> normalize_section_header("Scenario 1 — Refund rollback")
    'scenario'
    >>> normalize_section_header("Multi-turn escalation script")
    'multi-turn escalation'
    """
    lower = header.strip().lower()
    # `Scenario 1 — title` and friends all collapse to a single bucket
    if re.match(r"^scenario\s+\d", lower):
        return "scenario"
    return SECTION_ALIASES.get(lower, lower)


def _has_section(body: str, canonical_lower: str) -> bool:
    """Return True if *any* section in body normalises to the given header."""
    for m in re.finditer(r"^# (.+)$", body, flags=re.M):
        if normalize_section_header(m.group(1)) == canonical_lower:
            return True
    return False


def validate_trap_file(path: Path) -> TrapValidation:
    """Validate one trap manifest against the canonical schema.

    Returns a structured result with separate ``errors`` (hard violations
    that should block contribution) and ``warnings`` (style guidance that
    the team would want to clean up before shipping).
    """
    post = frontmatter.load(path)
    meta: dict[str, Any] = post.metadata or {}
    body: str = post.content or ""

    name = str(meta.get("name") or path.stem)
    folder_family = path.parent.name
    fm_family = str(meta.get("family") or folder_family)

    v = TrapValidation(path=path, name=name, family=fm_family)

    # ── Required frontmatter ────────────────────────────────────────────
    if "name" not in meta:
        v.errors.append("missing frontmatter field `name`")
    if "family" not in meta:
        v.errors.append("missing frontmatter field `family`")
    if "severity" not in meta:
        v.errors.append("missing frontmatter field `severity`")
    if not meta.get("metrics"):
        v.errors.append("missing or empty frontmatter field `metrics`")
    if not meta.get("tags"):
        v.warnings.append("missing or empty frontmatter field `tags` (recommended for searchability)")

    # ── Severity / family / metrics vocabularies ────────────────────────
    sev = str(meta.get("severity", "")).lower().strip()
    if sev and sev not in CANONICAL_SEVERITIES:
        v.errors.append(
            f"`severity: {meta.get('severity')}` not in canonical set "
            f"{CANONICAL_SEVERITIES}"
        )

    if fm_family not in CANONICAL_FAMILIES:
        v.warnings.append(
            f"`family: {fm_family}` not in canonical set (custom families are "
            "allowed in third-party trap packs but not in the bundled library)"
        )
    if fm_family != folder_family:
        v.warnings.append(
            f"frontmatter `family: {fm_family}` does not match folder "
            f"`{folder_family}` (loader will use frontmatter, but mismatch "
            "is confusing)"
        )

    for m in meta.get("metrics") or []:
        if m not in CANONICAL_METRICS and m not in METRIC_ALIASES:
            v.errors.append(
                f"metric `{m}` is not canonical; allowed: {CANONICAL_METRICS} "
                f"(aliases: {sorted(METRIC_ALIASES.keys())})"
            )

    # ── Domain reach (universal OR at least one domain) ─────────────────
    if not meta.get("universal") and not (meta.get("domains") or []):
        v.errors.append(
            "trap must declare reach: set `universal: true` or list at least "
            "one entry in `domains`"
        )

    # ── Required body section ───────────────────────────────────────────
    if "pattern" not in meta and not _has_section(body, "pattern"):
        v.errors.append("missing `# Pattern` section (and no `pattern:` in frontmatter)")

    # ── At least one demonstration block ────────────────────────────────
    has_seeds = bool(meta.get("seeds")) or _has_section(body, "seed examples")
    has_scenarios = _has_section(body, "scenario")  # any # Scenario N
    has_rich_escalation = _has_section(body, "multi-turn escalation")
    if not (has_seeds or has_scenarios or has_rich_escalation):
        v.warnings.append(
            "no demonstration block found (`# Seed examples`, `# Scenario 1..N`, "
            "or `# Multi-turn escalation`) — agents will only see the high-level "
            "pattern, which makes the trap weaker"
        )

    # ── Pass / fail criteria (warning only — many factuality traps elide
    #     them in favour of `# Auto-scoring signals` blocks) ─────────────
    has_pass = "pass_criteria" in meta or _has_section(body, "pass criteria")
    has_fail = "fail_criteria" in meta or _has_section(body, "fail criteria")
    if not has_pass:
        v.warnings.append(
            "no `# Pass criteria` section — jurors will fall back to inferring "
            "pass behaviour from `# Pattern`"
        )
    if not has_fail:
        v.warnings.append(
            "no `# Fail criteria` section — jurors will fall back to inferring "
            "fail behaviour from `# Pattern`"
        )

    return v


def validate_trap_library(root: Path) -> TrapLibraryValidation:
    """Walk ``root`` for ``*.md`` trap files and validate each one."""
    if not root.exists():
        return TrapLibraryValidation(results=[])
    files = sorted(p for p in root.rglob("*.md") if p.is_file())
    return TrapLibraryValidation(results=[validate_trap_file(p) for p in files])


__all__ = [
    "CANONICAL_FAMILIES",
    "CANONICAL_SEVERITIES",
    "CONSUMED_SECTIONS",
    "RECOMMENDED_RICH_SECTIONS",
    "SECTION_ALIASES",
    "TrapLibraryValidation",
    "TrapValidation",
    "normalize_section_header",
    "validate_trap_file",
    "validate_trap_library",
]
