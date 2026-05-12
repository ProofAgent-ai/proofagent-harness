"""Execute each bundled notebook end-to-end and report pass/fail per notebook.

Requires ANTHROPIC_API_KEY in the environment (notebooks will use it directly,
skipping the interactive getpass prompt). Each notebook makes real LLM calls.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    .venv/bin/python scripts/test_notebooks.py

Optional flags:
    --only 04          # run only notebook 04
    --timeout 600      # max seconds per notebook (default 300)
    --keep-output      # save executed notebooks to notebooks/_executed_*.ipynb
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

NB_DIR = Path(__file__).resolve().parent.parent / "notebooks"


def run_notebook(path: Path, timeout: int, keep_output: bool) -> tuple[bool, float, str]:
    """Execute a single notebook in-process via nbclient. Return (ok, seconds, msg)."""
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError

    nb = nbformat.read(path, as_version=4)
    client = NotebookClient(
        nb,
        timeout=timeout,
        kernel_name="python3",
        resources={"metadata": {"path": str(path.parent)}},
    )

    t0 = time.time()
    try:
        client.execute()
    except CellExecutionError as exc:
        elapsed = time.time() - t0
        # Find the failing cell index for a tighter error report
        first_err = next(
            (
                (i, "".join(c.source))
                for i, c in enumerate(nb.cells)
                if c.cell_type == "code"
                and any(
                    o.get("output_type") == "error" for o in c.get("outputs", [])
                )
            ),
            (None, ""),
        )
        cell_idx, cell_src = first_err
        msg = (
            f"failed in cell {cell_idx} after {elapsed:.1f}s\n"
            f"        ...{cell_src[:200].strip()}...\n"
            f"        error: {str(exc).splitlines()[0]}"
        )
        return False, elapsed, msg
    except Exception as exc:  # noqa: BLE001
        return False, time.time() - t0, f"runtime error: {exc}"

    elapsed = time.time() - t0
    if keep_output:
        out = path.with_name(f"_executed_{path.name}")
        nbformat.write(nb, out)
    return True, elapsed, "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Run only notebooks whose name starts with this prefix (e.g. '04').")
    parser.add_argument("--timeout", type=int, default=300, help="Max seconds per notebook (default 300).")
    parser.add_argument("--keep-output", action="store_true", help="Save executed notebooks to _executed_<name>.ipynb")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ANTHROPIC_API_KEY is not set. Export it first:")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        return 2

    notebooks = sorted(NB_DIR.glob("*.ipynb"))
    if args.only:
        notebooks = [p for p in notebooks if p.name.startswith(args.only)]
    if not notebooks:
        print(f"No notebooks matched (filter: {args.only!r}).")
        return 1

    print(f"Executing {len(notebooks)} notebook(s) with timeout={args.timeout}s\n")

    results: list[tuple[Path, bool, float, str]] = []
    for nb_path in notebooks:
        print(f"  > {nb_path.name} ", end="", flush=True)
        ok, secs, msg = run_notebook(nb_path, args.timeout, args.keep_output)
        results.append((nb_path, ok, secs, msg))
        print(f"{'PASS' if ok else 'FAIL'} ({secs:.1f}s)")
        if not ok:
            for line in msg.splitlines():
                print(f"      {line}")

    print("\n" + "=" * 60)
    n_pass = sum(1 for _, ok, _, _ in results if ok)
    n_fail = len(results) - n_pass
    total_time = sum(s for _, _, s, _ in results)
    print(f"  Summary: {n_pass} passed, {n_fail} failed in {total_time:.1f}s total")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
