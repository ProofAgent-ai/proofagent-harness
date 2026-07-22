"""Governance integration — turn a finished evaluation into a release gate.

This example shows the *last mile* of the harness: you've run an adversarial
evaluation, you have a :class:`~proofagent_harness.schemas.Report`, and now you
want to push it to the **ProofAgent Governance API** so it can gate a release
(pass / review / block) and land on the governance dashboard with full fidelity.

It runs WITHOUT any LLM API key. It loads a saved report from ``results/`` —
produced by an earlier run of any example or ``proof run`` (a fresh clone has
none yet: run ``01_quickstart.py`` first, or point ``--report`` at any saved
Report JSON) — builds the upload payload, and prints a summary. Only the
optional final step — actually POSTing the run — needs governance credentials,
and only happens when you pass ``--upload``.

──────────────────────────────────────────────────────────────────────────────
Everything is driven from the terminal
──────────────────────────────────────────────────────────────────────────────
``--report``          which saved report JSON to upload (default: auto-pick the
                      richest one under ``results/``).
``--agent`` /         the release-gate identity the backend groups + gates runs
``--agent-version``   by.
``--profile``         governance profile slug to evaluate against.
``--source``          origin of the run (local | ci_cd | manual | api | scheduled).
``--fail-on``         which gate decision fails the build (pass | review | block).
``--upload`` /        opt in to the network call. Off by default — offline, the
``--no-upload``       example writes the payload it WOULD send to
                      ``examples/_governance_payload.sample.json`` for inspection.
``--api-key``         credentials. The flag wins over the PROOFAGENT_API_KEY env
                      var. Uploads always go to ProofAgent Cloud.

──────────────────────────────────────────────────────────────────────────────
Run it
──────────────────────────────────────────────────────────────────────────────
Offline (uses a bundled saved report — no keys, no network):

    python examples/11_governance_gate.py

Upload + gate a release (Cloud by default):

    python examples/11_governance_gate.py \\
        --upload --api-key pa_live_... \\
        --agent "Refund Agent" --agent-version v1.8.2 \\
        --profile airline_customer_support --source ci_cd --fail-on block

──────────────────────────────────────────────────────────────────────────────
The gate, in CI
──────────────────────────────────────────────────────────────────────────────
With ``--upload``, this script EXITS with the gate-mapped code (0 pass / 1
review / 2 block, subject to ``--fail-on``) — wire it straight into a CI step to
fail the build on a bad gate. Internally that is just::

    from proofagent_harness.governance import gate_exit_code
    sys.exit(gate_exit_code(decision["gate_status"], fail_on="block"))

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from proofagent_harness import Report
from proofagent_harness.governance import (
    DEFAULT_API_BASE_URL,
    build_governance_payload,
    gate_exit_code,
    upload_run,
)

# Repo root = parent of this examples/ directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"
SAMPLE_OUT = Path(__file__).resolve().parent / "_governance_payload.sample.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--report", "-r", default=None, metavar="PATH",
        help="Saved Report JSON to upload. Default: auto-pick the richest "
             "report under results/ (prefers a multi-turn run with a transcript).",
    )
    # ── Release-gate identity + gate behavior ──
    p.add_argument(
        "--agent", default="Refund Agent", metavar="NAME",
        help="Logical agent name (groups runs + regressions). Default: 'Refund Agent'.",
    )
    p.add_argument(
        "--agent-version", default="v1.8.2", metavar="VER",
        help="Version / git ref of the agent under test. Default: v1.8.2.",
    )
    p.add_argument(
        "--profile", default="airline_customer_support", metavar="SLUG",
        help="Governance profile slug to gate against. "
             "Default: airline_customer_support.",
    )
    p.add_argument(
        "--source", default="manual",
        choices=["local", "ci_cd", "manual", "api", "scheduled"],
        help="Origin of this run, recorded in the dashboard. Default: manual.",
    )
    p.add_argument(
        "--fail-on", default="block", choices=["pass", "review", "block"],
        help="Which gate decision fails the build (exit non-zero). Default: block.",
    )
    # ── Upload toggle + credentials (off by default — runs fully offline) ──
    p.add_argument(
        "--upload", action="store_true", default=False,
        help="POST the run to the Governance API and exit with the gate code. "
             "Off by default — offline, the payload is written to "
             f"{SAMPLE_OUT.name} for inspection.",
    )
    p.add_argument(
        "--no-upload", dest="upload", action="store_false",
        help="Explicitly run offline (the default).",
    )
    p.add_argument(
        "--api-key", default=None, metavar="KEY",
        help="Governance API key. Defaults to env PROOFAGENT_API_KEY.",
    )
    return p.parse_args()


def _looks_multi_turn(path: Path) -> bool:
    """Cheap check: does this JSON report have a non-empty multi-turn transcript?

    We only peek at a couple of fields so we don't pay to parse a 250 KB report
    just to reject it.
    """
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if str(data.get("mode", "multi_turn")) != "multi_turn":
        return False
    return bool(data.get("transcript"))


def _find_report_json() -> Path:
    """Pick a real saved report from ``results/``.

    Preference order, so the printed summary is as rich as possible:
      1. a multi-turn report with an actual transcript (best demo), then
      2. any ``*.json`` report file under ``results/`` (artifact mode is fine —
         ``build_governance_payload`` handles both).
    Searches recursively because runs land in dated sub-folders.
    """
    if not RESULTS_DIR.is_dir():
        raise SystemExit(
            f"No results/ directory at {RESULTS_DIR}.\n"
            "Run an evaluation first (see examples/01_quickstart.py), or point "
            "this script at any saved Report JSON via --report."
        )

    candidates = sorted(RESULTS_DIR.rglob("*.json"))
    if not candidates:
        raise SystemExit(
            f"No *.json reports under {RESULTS_DIR}.\n"
            "Run an evaluation first (e.g. `proof run ...`) so a report is saved."
        )

    for path in candidates:
        if _looks_multi_turn(path):
            return path
    # Fall back to any report JSON.
    return candidates[0]


def _resolve_report_path(arg: str | None) -> Path:
    """Honour --report when given; otherwise auto-pick from results/."""
    if arg:
        p = Path(arg).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"--report path does not exist: {p}")
        return p
    return _find_report_json()


def _print_payload_summary(payload: dict[str, Any], report_path: Path) -> None:
    """Human-readable summary of what the payload carries."""
    token_usage = payload.get("token_usage", {}) or {}
    consensus = payload.get("consensus", {}) or {}
    events = payload.get("events", []) or []
    trace_steps = payload.get("trace_steps", []) or []
    findings = payload.get("findings", []) or []
    summary = payload.get("summary", {}) or {}

    print("=" * 70)
    print("Governance payload built from a saved report")
    print("=" * 70)
    print(f"source report      : {report_path}")
    print(f"agent              : {payload['agent'].get('name')} "
          f"({payload['agent'].get('version', 'no version')})")
    print(f"profile            : {payload.get('governance_profile', '(none)')}")
    print(f"mode               : {payload.get('mode')}")
    print(f"source             : {payload.get('source')}")
    print("-" * 70)
    print(f"final_score        : {summary.get('final_score')}")
    print(f"grade_label        : {summary.get('grade_label')}")
    print(f"certification      : {payload.get('certification')}")
    print(f"production_ready    : {payload.get('production_ready') or '(n/a)'}")
    print("-" * 70)
    print(f"#trace_steps       : {len(trace_steps)}")
    print(f"#findings          : {len(findings)}")
    print(f"#events            : {len(events)}")
    print(f"#consensus metrics : {len(consensus)}")
    print(f"executive_summary  : {'present' if payload.get('executive_summary') else 'absent'}")
    print(f"consensus log      : {'present' if consensus else 'absent'}")
    print("-" * 70)
    print("token_usage:")
    print(f"  total            : {token_usage.get('total_tokens')}")
    print(f"  input / output   : {token_usage.get('input_tokens')} / "
          f"{token_usage.get('output_tokens')}")
    print(f"  primary calls    : {token_usage.get('primary_call_count')}")
    print(f"  fallback calls   : {token_usage.get('fallback_call_count')} "
          f"(rate {token_usage.get('fallback_rate')})")
    print("=" * 70)


def main() -> int:
    args = parse_args()
    report_path = _resolve_report_path(args.report)

    # Load the saved report — NO LLM key needed, this is pure deserialization.
    report = Report.model_validate(json.loads(report_path.read_text()))

    # Build the upload payload. agent/version/profile/source are the
    # release-gate context the governance backend groups and gates runs by.
    payload = build_governance_payload(
        report,
        agent_name=args.agent,
        agent_version=args.agent_version,
        profile=args.profile,
        source=args.source,
    )

    _print_payload_summary(payload, report_path)

    # Credentials: flag wins, env is the fallback. Upload target is hard-locked
    # to ProofAgent Cloud.
    api_key = args.api_key or os.environ.get("PROOFAGENT_API_KEY")
    api_url = DEFAULT_API_BASE_URL

    if not args.upload:
        SAMPLE_OUT.write_text(json.dumps(payload, indent=2))
        print()
        print("[offline] --upload not set — nothing was sent.")
        print(f"Wrote the payload that WOULD be uploaded to: "
              f"{SAMPLE_OUT.relative_to(REPO_ROOT)}")
        print()
        print("To upload + gate (uploads to ProofAgent Cloud):")
        print("  python examples/11_governance_gate.py --upload \\")
        print('      --api-key pa_live_...   # or set PROOFAGENT_API_KEY')
        return 0

    if not api_key:
        print()
        print("--upload was set but no API key was provided. Pass --api-key or "
              "set PROOFAGENT_API_KEY (Dashboard → Settings → API Keys).")
        return 2

    # ── Upload + gate ──
    print()
    print(f"Uploading run to {api_url} ...")
    decision = upload_run(payload, api_url=api_url, api_key=api_key)

    gate_status = str(decision.get("gate_status", "unknown"))
    print(f"gate decision      : {gate_status}")
    print(f"grade_label        : {decision.get('grade_label')}")
    print(f"final_score        : {decision.get('final_score')}")
    print(f"dashboard_url      : {decision.get('dashboard_url')}")
    failed = decision.get("failed_rules") or []
    if failed:
        print(f"failed_rules       : {failed}")

    # In CI this exit code fails the build on a bad gate (per --fail-on).
    return gate_exit_code(gate_status, fail_on=args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
