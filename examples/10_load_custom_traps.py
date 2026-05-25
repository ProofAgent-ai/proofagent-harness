"""Minimal example — load custom traps into the harness, inspect, wire.

This example is **loading-only**. It makes ZERO LLM calls. The point is to
show the trap-loading API in isolation so you can:

  1. Confirm your `.md` files parse correctly before paying for an eval.
  2. See which custom traps merged on top of the bundled 64.
  3. Understand the `Harness(extra_traps=[...], trap_packs=[...])` wiring.

For the full end-to-end run that actually evaluates an agent against custom
traps, see ``examples/08_custom_trap.py``.

Author your own traps following ``docs/TRAP_MANIFEST.md``. A worked sample
ships at ``examples/custom_traps/refund_chargeback_threat.md``.

Usage
-----

    # Default — inspect the bundled refund_chargeback_threat sample trap
    python examples/10_load_custom_traps.py

    # Point at your own trap directory
    python examples/10_load_custom_traps.py --traps-dir ./my_traps/

    # Point at a single .md file
    python examples/10_load_custom_traps.py --traps-dir ./my_traps/foo.md
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Everything you need lives on the top-level package.
from proofagent_harness import Harness, TrapIndex, load_traps

EXAMPLE_DIR = Path(__file__).resolve().parent
DEFAULT_TRAPS_DIR = EXAMPLE_DIR / "custom_traps"


def resolve_traps_source(raw: str) -> tuple[Path, Path | None]:
    """Accept either a directory of .md files OR a single .md file.

    If a single file is passed, copy it into a temp dir so ``load_traps``'s
    directory walker can pick it up without surfacing sibling files.

    Returns ``(load_dir, cleanup_dir_or_None)``.
    """
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"--traps-dir path does not exist: {path}")
    if path.is_dir():
        if not list(path.rglob("*.md")):
            raise SystemExit(f"directory has no .md trap files: {path}")
        return path, None
    if path.suffix.lower() != ".md":
        raise SystemExit(f"single-file --traps-dir must end in .md: {path}")
    tmp = Path(tempfile.mkdtemp(prefix="proofagent_traps_demo_"))
    shutil.copy2(path, tmp / path.name)
    return tmp, tmp


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Minimal trap-loading demo — inspect bundled + custom traps "
            "and show how they wire into Harness. Zero LLM calls."
        ),
    )
    p.add_argument(
        "--traps-dir", "-t", default=str(DEFAULT_TRAPS_DIR),
        help=(
            "Directory of .md trap manifests OR a single .md file. "
            f"Default: {DEFAULT_TRAPS_DIR}"
        ),
    )
    p.add_argument(
        "--family", "-f", default=None,
        help="Optional: filter the printed inventory to a single family "
             "(e.g. prompt_injection, social_engineering, tool_misuse).",
    )
    p.add_argument(
        "--metric", "-m", default=None,
        help="Optional: filter the printed inventory to traps that score "
             "a specific metric (e.g. safety, manipulation_resistance).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    traps_dir, cleanup_dir = resolve_traps_source(args.traps_dir)

    try:
        # ── 1. Load: bundled library + your custom directory ──────────────
        bundled = load_traps()
        merged = load_traps(extra_dirs=[str(traps_dir)])

        custom_names = sorted({t.name for t in merged} - {t.name for t in bundled})
        if not custom_names:
            print(f"[note] {traps_dir} added 0 NEW traps "
                  f"(names may collide with bundled — last wins).")
        else:
            print(f"[load] {traps_dir.name}/ added {len(custom_names)} NEW trap(s):")
            for n in custom_names:
                print(f"         + {n}")

        print(f"[load] bundled = {len(bundled)}  |  "
              f"merged total = {len(merged)}")

        # ── 2. Index: pre-built lookup tables for fast filtering ──────────
        index = TrapIndex(merged)

        print("\n[index] by family:")
        for fam in sorted(index.by_family):
            print(f"          {fam:<22} {len(index.by_family[fam])} trap(s)")

        print(f"\n[index] universal (apply to any agent): "
              f"{len(index.universals)}")
        print(f"[index] domain-scoped:                  "
              f"{len(index.domain_specific)}")

        # ── 3. Optional filtered inventory ────────────────────────────────
        if args.family or args.metric:
            print("\n[filter] traps matching:", end=" ")
            if args.family:
                print(f"family={args.family}", end=" ")
            if args.metric:
                print(f"metric={args.metric}", end=" ")
            print()
            for t in merged:
                if args.family and t.family != args.family:
                    continue
                if args.metric and args.metric not in (t.metrics or []):
                    continue
                origin = "custom" if t.name in custom_names else "bundled"
                print(f"  - {t.name:<40} [{t.family}/{t.severity}/{origin}]")

        # ── 4. Wire into Harness (no .evaluate() call here — this is a
        #     loading demo). The constructor calls load_traps() internally
        #     using the same arguments, so the conductor sees the same
        #     library you just printed.
        _harness = Harness(
            llm="claude-sonnet-4-6",          # any LiteLLM-compatible target
            extra_traps=[str(traps_dir)],     # one or more directories
            # trap_packs=["finance"],         # OR pip-installed pack:
            #                                 # proofagent_traps_finance
            turns=8,
            consensus="delphi",
        )
        print(f"\n[wire] Harness ready with "
              f"{len(_harness._traps)} trap(s) in the conductor library.")
        print("[wire] Drop this script into a real eval by calling "
              "_harness.evaluate(your_agent, ...).")
        print("       See examples/01_quickstart.py and "
              "examples/08_custom_trap.py for full end-to-end runs.")

        return 0
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
