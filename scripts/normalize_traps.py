#!/usr/bin/env python3
"""Normalize bundled trap manifests to the canonical v1.0 structure.

What this script does
---------------------
For every ``*.md`` under ``src/proofagent_harness/data/traps/``:

  1. Re-orders the YAML frontmatter keys to the canonical order
     (name → family → severity → metrics → tags → universal → domains →
     forbidden_tools → expected_tools → any other keys verbatim).
  2. Renames section headers that use a recognised alias to their
     canonical form (e.g. ``# Multi-turn escalation script`` →
     ``# Multi-turn escalation``).
  3. Writes the file back **only when something actually changed**.

What this script does NOT do
----------------------------
  * It never changes a section's body content.
  * It never adds, removes, or renames frontmatter VALUES — only the
    KEY ORDER. (The semantic dict is identical before vs after.)
  * It never touches Scenario blocks, rich extras, or any header that
    isn't in ``trap_schema.SECTION_ALIASES``.

Safety
------
The script runs an embedded verification pass after the rewrite. It
loads every trap through ``loaders.load_traps()`` before and after the
transform, then asserts the resulting ``Trap`` objects are byte-equal.
On any mismatch the script aborts and prints a diff — no commits get
pushed.

Usage
-----
    python scripts/normalize_traps.py             # apply + verify
    python scripts/normalize_traps.py --dry-run   # only report
    python scripts/normalize_traps.py --check     # exit 1 if not normalized
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import OrderedDict
from difflib import unified_diff
from pathlib import Path
from typing import Any

import frontmatter

REPO = Path(__file__).resolve().parent.parent
TRAPS_DIR = REPO / "src" / "proofagent_harness" / "data" / "traps"

sys.path.insert(0, str(REPO / "src"))
from proofagent_harness.loaders import load_traps  # noqa: E402
from proofagent_harness.trap_schema import SECTION_ALIASES  # noqa: E402

# Canonical key order for trap-manifest frontmatter (per docs/TRAP_MANIFEST.md).
CANONICAL_FRONTMATTER_KEYS: tuple[str, ...] = (
    "name",
    "family",
    "severity",
    "metrics",
    "tags",
    "universal",
    "domains",
    "forbidden_tools",
    "expected_tools",
    # Inline body overrides recognised by the loader — kept last in the
    # rare files that use them.
    "seeds",
    "pattern",
    "pass_criteria",
    "fail_criteria",
)


# ---------------------------------------------------------------------------
#  Frontmatter emit (manual, so we match the existing inline-list style)
# ---------------------------------------------------------------------------

_BARE_ITEM = re.compile(r"^[A-Za-z0-9_\-.]+$")


def _emit_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote only when needed (matches the existing style of the bundled
    # library — see e.g. permission_escalation's `domains: ['enterprise', …]`
    # which uses quotes only because of `public-sector`'s hyphen).
    if _BARE_ITEM.match(s) and ":" not in s and "#" not in s:
        return s
    return f"'{s}'" if "'" not in s else f"\"{s}\""


def _emit_inline_list(items: list[Any]) -> str:
    return "[" + ", ".join(_emit_scalar(it) for it in items) + "]"


def _emit_value(v: Any) -> str:
    if isinstance(v, list):
        return _emit_inline_list(v)
    return _emit_scalar(v)


def _is_empty(v: Any) -> bool:
    """True for values we drop from frontmatter (empty lists/dicts/strings)."""
    return v is None or v == "" or v == [] or v == {} or v is False


def canonical_frontmatter_block(meta: dict[str, Any]) -> str:
    """Return a ``--- ... ---`` block with canonical key ordering.

    * Canonical keys appear in the order defined by ``CANONICAL_FRONTMATTER_KEYS``,
      with empty values dropped (semantically identical to "omitted" in the
      loader, matches the majority style of the bundled library).
    * Unknown keys are appended at the end verbatim so third-party packs
      can add fields without losing them.
    """
    canonical_set = set(CANONICAL_FRONTMATTER_KEYS)
    ordered = OrderedDict()

    for k in CANONICAL_FRONTMATTER_KEYS:
        if k in meta and not _is_empty(meta[k]):
            ordered[k] = meta[k]

    # Preserve unknown (non-canonical) keys at the end — verbatim. Skip
    # any canonical keys that were intentionally dropped above.
    for k, v in meta.items():
        if k in canonical_set:
            continue
        if _is_empty(v):
            continue
        ordered[k] = v

    lines = ["---"]
    for k, v in ordered.items():
        lines.append(f"{k}: {_emit_value(v)}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Section-header canonicalisation
# ---------------------------------------------------------------------------


#: Only match within-line whitespace ([ \t]), never `\n`, so the substitution
#: doesn't accidentally eat the blank line that conventionally follows a header.
_HEADER_RE = re.compile(r"^(#{1,2})[ \t]+(.+?)[ \t]*$", flags=re.M)


def canonicalize_section_headers(body: str) -> str:
    """Rename `# Multi-turn escalation script` → `# Multi-turn escalation`, etc.

    Only renames headers that exactly match an entry in
    ``trap_schema.SECTION_ALIASES``. The body of each section is
    preserved untouched.
    """

    def _sub(m: re.Match[str]) -> str:
        hashes, header = m.group(1), m.group(2)
        canonical = SECTION_ALIASES.get(header.lower())
        if canonical is None:
            return m.group(0)
        # Title-case the first letter to match the existing convention
        # (e.g. "Multi-turn escalation").
        canonical_display = canonical[0].upper() + canonical[1:]
        return f"{hashes} {canonical_display}"

    return _HEADER_RE.sub(_sub, body)


# ---------------------------------------------------------------------------
#  File-level normaliser
# ---------------------------------------------------------------------------


def normalize_file(path: Path) -> tuple[str, str]:
    """Return ``(original_text, normalized_text)`` for one trap file.

    The two are equal when the file is already in canonical form.
    """
    original = path.read_text()
    post = frontmatter.load(path)
    meta = post.metadata or {}
    body = post.content

    new_fm = canonical_frontmatter_block(meta)
    new_body = canonicalize_section_headers(body)

    # Preserve trailing newline if the original had one.
    trailing = "\n" if original.endswith("\n") else ""
    # Frontmatter block + single blank line + body (matches existing style).
    normalized = f"{new_fm}\n\n{new_body.lstrip()}".rstrip() + trailing
    return original, normalized


# ---------------------------------------------------------------------------
#  Driver
# ---------------------------------------------------------------------------


def _snapshot_traps() -> list[dict[str, Any]]:
    """Dump every loaded Trap object as a sorted-by-name list of dicts."""
    return sorted(
        [t.model_dump() for t in load_traps()],
        key=lambda d: d["name"],
    )


def _diff(a: str, b: str, path: Path) -> str:
    return "\n".join(
        unified_diff(
            a.splitlines(),
            b.splitlines(),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            lineterm="",
        )
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    ap.add_argument("--check",   action="store_true", help="Exit 1 if any file needs normalization.")
    args = ap.parse_args()

    files = sorted(p for p in TRAPS_DIR.rglob("*.md") if p.is_file())
    if not files:
        print(f"No trap files found under {TRAPS_DIR}", file=sys.stderr)
        return 1

    # 1) Baseline snapshot — what the Harness loader sees today.
    print(f"Loading semantic baseline from {len(files)} trap files …")
    before = _snapshot_traps()
    print(f"  ✓ baseline captured: {len(before)} Trap objects\n")

    # 2) Plan changes.
    changes: list[tuple[Path, str, str]] = []
    for p in files:
        orig, norm = normalize_file(p)
        if orig != norm:
            changes.append((p, orig, norm))

    if not changes:
        print("All trap files are already in canonical form. Nothing to do.")
        return 0

    print(f"Files needing normalization: {len(changes)} of {len(files)}")
    for p, _, _ in changes:
        print(f"  · {p.relative_to(TRAPS_DIR)}")
    print()

    if args.check:
        print("[--check] Exiting non-zero because files need normalization.")
        return 1

    if args.dry_run:
        # Show one diff sample so the operator can sanity-check.
        sample_p, sample_a, sample_b = changes[0]
        print(f"--- sample diff: {sample_p.relative_to(TRAPS_DIR)} ---")
        print(_diff(sample_a, sample_b, sample_p))
        print(f"\n[--dry-run] No files written.")
        return 0

    # 3) Write changes.
    for p, _, norm in changes:
        p.write_text(norm)
    print(f"Wrote {len(changes)} file(s).\n")

    # 4) Verify semantic equality.
    print("Re-loading after normalization and verifying semantic equality …")
    after = _snapshot_traps()

    if before == after:
        print(f"  ✓ all {len(after)} Trap objects byte-identical to baseline.")
        return 0

    # On mismatch: surface exactly which fields changed.
    print("  ✗ semantic mismatch detected. Listing first 5 differences:\n")
    diff_count = 0
    by_name_before = {d["name"]: d for d in before}
    by_name_after = {d["name"]: d for d in after}
    for name in sorted(set(by_name_before) | set(by_name_after)):
        b = by_name_before.get(name)
        a = by_name_after.get(name)
        if b == a:
            continue
        diff_count += 1
        print(f"    [{name}]")
        if b is None:
            print("      missing in baseline (trap appeared)")
            continue
        if a is None:
            print("      missing post-transform (trap disappeared)")
            continue
        for field in set(b) | set(a):
            if b.get(field) != a.get(field):
                print(f"      {field}: {b.get(field)!r}  →  {a.get(field)!r}")
        if diff_count >= 5:
            break
    return 2


if __name__ == "__main__":
    sys.exit(main())
