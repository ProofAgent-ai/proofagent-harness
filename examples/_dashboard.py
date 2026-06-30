"""Optional dashboard push — a tiny helper shared by the runnable examples.

Every example can OPTIONALLY push its finished evaluation to the **ProofAgent
Governance dashboard**. This is the *same* path the ``proof ... --upload`` CLI
uses (``build_governance_payload`` → ``upload_run`` → gate decision), wrapped in
one no-op-safe call so an example still runs **fully offline** when no
credentials are given.

Two ways to supply credentials — the base URL defaults to **ProofAgent Cloud**,
so you only need an API key (works for BOTH multi-turn and artifact runs):

  1. CLI flags (preferred in the examples — explicit, no shell exports needed)::

         python examples/01_quickstart.py --upload --api-key pa_live_...
         # on-prem: ... --upload --api-key pa_live_... --api-url https://proofagent.acme.internal

  2. Environment variables (used as the fallback when a flag is omitted)::

         export PROOFAGENT_API_KEY="pa_live_..."          # Dashboard → Settings → API Keys
         export PROOFAGENT_API_BASE_URL="https://proofagent.acme.internal"   # on-prem only

When ``push_to_dashboard`` is called with no key (neither flag nor env), it
prints a one-line hint and returns ``None`` — the example's local JSON /
Markdown report is unaffected. With a key, it builds the governance payload,
POSTs it (to Cloud, or your override), and prints the gate decision + dashboard
URL.

The examples gate the call behind an explicit ``--upload`` flag (default
``--no-upload``) so the upload/offline choice is a deliberate two-option UX, not
"happens to push because an env key is present". Use :func:`add_governance_upload_args`
to register that flag group, then forward ``args`` straight into
:func:`push_to_dashboard` (``api_key=args.api_key``, ``api_url=args.api_url``, …).

Inline equivalent (if you'd rather not import this helper) — five lines::

    import os
    from proofagent_harness.governance import build_governance_payload, upload_run
    if os.getenv("PROOFAGENT_API_KEY"):                       # base URL defaults to Cloud
        payload = build_governance_payload(report, agent_name="my-agent", source="manual")
        decision = upload_run(payload, api_key=os.environ["PROOFAGENT_API_KEY"])
        print(decision["gate_status"], decision.get("dashboard_url"))

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import argparse

    from proofagent_harness.schemas import Report


def dashboard_enabled() -> bool:
    """True when an *env-driven* push would be attempted — i.e.
    ``PROOFAGENT_API_KEY`` is set.

    The base URL defaults to ProofAgent Cloud, so the key alone is enough;
    ``PROOFAGENT_API_BASE_URL`` only overrides it for Enterprise / on-prem.

    Note: the examples drive the push from the ``--upload`` flag (see
    :func:`add_governance_upload_args`), not from this env check — so an
    example pushes only when you ask it to.
    """
    return bool(os.getenv("PROOFAGENT_API_KEY"))


def add_governance_upload_args(
    parser: "argparse.ArgumentParser",
    *,
    default_agent: str | None = None,
    include_agent_flag: bool = True,
) -> None:
    """Register the uniform governance-upload flag group on ``parser``.

    Gives every example an explicit, two-option upload/offline UX::

        --upload / --no-upload   (default: --no-upload — run fully offline)
        --api-key                (default: env PROOFAGENT_API_KEY)
        --api-url                (default: env PROOFAGENT_API_BASE_URL, then Cloud)
        --agent                  (logical agent name; default: ``default_agent``)
        --agent-version          (git ref / version of the agent under test)
        --profile                (governance profile slug to gate against)
        --fail-on                (pass | review | block — default block)
        --source                 (local | ci_cd | manual | api | scheduled)

    After parsing, forward straight into :func:`push_to_dashboard` *only when*
    ``args.upload`` is true::

        if args.upload:
            push_to_dashboard(
                report,
                agent_name=args.agent or "my-agent",
                agent_version=args.agent_version,
                profile=args.profile,
                source=args.source,
                fail_on=args.fail_on,
                api_key=args.api_key,
                api_url=args.api_url,
            )

    Flag values take precedence over the env vars; the env vars remain the
    fallback so an exported key still works without re-typing ``--api-key``.

    ``include_agent_flag=False`` skips the ``--agent`` flag — pass it when the
    example already defines its own ``--agent`` for a different purpose (e.g.
    08_live_trace.py uses ``--agent`` to pick which agent spec to load). The
    dashboard agent name is then derived by the example (``args.agent`` is *not*
    referenced for the push in that case).
    """
    g = parser.add_argument_group(
        "governance upload (optional — off by default)",
        "Push the finished run to the ProofAgent Governance API and gate on the "
        "returned decision. Same path as `proof run --upload`. Omit --upload to "
        "stay fully offline (the local JSON/Markdown report is always written).",
    )
    g.add_argument(
        "--upload", action="store_true", default=False,
        help="Upload this run to the Governance API + print the gate decision. "
             "Off by default (offline).",
    )
    g.add_argument(
        "--no-upload", dest="upload", action="store_false",
        help="Explicitly run offline (the default).",
    )
    g.add_argument(
        "--api-key", default=None, metavar="KEY",
        help="Governance API key. Defaults to env PROOFAGENT_API_KEY.",
    )
    g.add_argument(
        "--api-url", default=None, metavar="URL",
        help="Governance API base URL. Defaults to env PROOFAGENT_API_BASE_URL, "
             "then ProofAgent Cloud. Override only for Enterprise / on-prem.",
    )
    if include_agent_flag:
        g.add_argument(
            "--agent", default=default_agent, metavar="NAME",
            help="Logical agent name (groups runs + regressions in the dashboard)."
                 + (f" Default: {default_agent}." if default_agent else ""),
        )
    g.add_argument(
        "--agent-version", default=None, metavar="VER",
        help="Version / git ref of the agent under test.",
    )
    g.add_argument(
        "--profile", default=None, metavar="SLUG",
        help="Governance profile slug to evaluate against (e.g. "
             "airline_customer_support).",
    )
    g.add_argument(
        "--fail-on", default="block", choices=["pass", "review", "block"],
        help="Which gate decision fails the build (advisory _exit_code only; "
             "the example does not sys.exit on it). Default: block.",
    )
    g.add_argument(
        "--source", default="local",
        choices=["local", "ci_cd", "manual", "api", "scheduled"],
        help="Origin of this run, recorded in the dashboard. Default: local.",
    )


def push_to_dashboard(
    report: "Report",
    *,
    agent_name: str,
    agent_version: str | None = None,
    profile: str | None = None,
    source: str = "manual",
    fail_on: str = "block",
    run_name: str | None = None,
    api_key: str | None = None,
    api_url: str | None = None,
) -> dict[str, Any] | None:
    """Optionally push a finished ``Report`` to the ProofAgent Governance dashboard.

    No-op (returns ``None``) unless a key is available — passed via ``api_key``
    or, as a fallback, the ``PROOFAGENT_API_KEY`` env var — so every example
    runs offline by default. The base URL defaults to ProofAgent Cloud; override
    it with ``api_url`` or the ``PROOFAGENT_API_BASE_URL`` env var for
    Enterprise / on-prem. When enabled, it builds the governance payload
    (identical to ``proof run --upload``), uploads it, prints the gate decision +
    dashboard URL, and returns the decision dict.

    **Never raises.** A transport / credentials error is printed, not
    propagated, so a demo push can't crash the example or mask the local report.

    Parameters mirror :func:`proofagent_harness.governance.build_governance_payload`:
    ``agent_name`` (groups runs + regressions in the dashboard), optional
    ``agent_version`` (git ref), ``profile`` (governance profile slug to gate
    against), ``source`` (``local`` | ``ci_cd`` | ``manual`` | ``api`` |
    ``scheduled``), and ``run_name`` (human label). ``fail_on`` only affects the
    advisory ``_exit_code`` added to the returned dict — this helper never calls
    ``sys.exit`` itself (CI examples like ``11_pytest_ci`` decide that).

    ``api_key`` and ``api_url``, when provided (e.g. wired from ``--api-key`` /
    ``--api-url`` flags), take **precedence** over the matching env vars, so the
    CLI flags work without the user exporting anything.

    Returns the gate-decision dict (``run_id``, ``gate_status``, ``grade_label``,
    ``final_score``, ``dashboard_url``, ``failed_rules`` …) with an extra
    ``_exit_code`` key, or ``None`` when disabled / failed.
    """
    # Flag value wins, then env. Keeps an exported key working without --api-key.
    api_key = api_key or os.getenv("PROOFAGENT_API_KEY")
    if not api_key:
        print(
            "[dashboard] no API key — skipping dashboard push "
            "(report saved locally only).\n"
            "[dashboard] pass --api-key (or set PROOFAGENT_API_KEY) to push this "
            "run (Cloud by default; use --api-url / PROOFAGENT_API_BASE_URL for "
            "an Enterprise / on-prem endpoint)."
        )
        return None

    # Imported lazily so an example still imports cleanly on an older harness
    # build that predates the governance module.
    try:
        from proofagent_harness.governance import (
            DEFAULT_API_BASE_URL,
            GovernanceUploadError,
            build_governance_payload,
            gate_exit_code,
            upload_run,
        )
    except ImportError:
        print(
            "[dashboard] this harness build has no governance module — "
            "upgrade with `pip install -U proofagent-harness` to push to the dashboard."
        )
        return None

    # Base URL defaults to ProofAgent Cloud — only an API key is required.
    # Flag value (api_url) wins over env, which wins over the Cloud default.
    api_url = api_url or os.getenv("PROOFAGENT_API_BASE_URL") or DEFAULT_API_BASE_URL

    payload = build_governance_payload(
        report,
        agent_name=agent_name,
        agent_version=agent_version,
        profile=profile,
        source=source,
        run_name=run_name,
    )
    try:
        decision = upload_run(payload, api_url=api_url, api_key=api_key)
    except GovernanceUploadError as exc:
        print(f"[dashboard] upload failed (run still saved locally): {exc}")
        return None

    gate = str(decision.get("gate_status", "unknown")).lower()
    grade = decision.get("grade_label", "n/a")
    print(
        f"[dashboard] gate: {gate.upper()}  ·  score {decision.get('final_score')} ({grade})"
    )
    if decision.get("dashboard_url"):
        print(f"[dashboard] view: {decision['dashboard_url']}")
    failed = decision.get("failed_rules") or []
    if failed:
        print(f"[dashboard] failed rules: {failed}")

    # Convenience: the CI exit code this decision maps to (the example decides
    # whether to act on it; this helper never exits the process itself).
    decision["_exit_code"] = gate_exit_code(gate, fail_on=fail_on)
    return decision
