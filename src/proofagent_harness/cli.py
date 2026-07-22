"""Typer CLI - the `proof` command."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import sys
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from proofagent_harness import (
    CANONICAL_METRICS,
    METRIC_DESCRIPTIONS,
    AgentContext,
    Harness,
    __version__,
)
from proofagent_harness.loaders import load_trap_index, load_traps

app = typer.Typer(
    name="proof",
    help="proofagent-harness - open-source test harness for AI agents.",
    no_args_is_help=True,
    add_completion=False,
)

traps_app = typer.Typer(name="traps", help="Manage trap libraries.", no_args_is_help=True)
app.add_typer(traps_app)

console = Console()


def _abort_on_agent_crash(exc) -> None:
    """Turn the conductor's AgentUnderTestError into a clean, actionable panel
    (never a raw traceback) and exit 2 — the agent under test is misconfigured,
    which is the USER's code, not a harness bug."""
    hint = ""
    if "temperature" in exc.last_error.lower():
        hint = (
            "\n\nThis model deprecated `temperature`. If your agent sends it, "
            "drop it for this model (see examples/credit_agent/agent.py's "
            "_agent_completion for the drop-and-retry pattern)."
        )
    console.print(Panel(
        f"[bold red]The agent under test crashed[/bold red] "
        f"{exc.consecutive} turns in a row with the same error "
        f"([yellow]{exc.exc_type}[/yellow]):\n\n  {exc.last_error}\n\n"
        f"The harness stopped so it wouldn't spend the jury on empty answers. "
        f"This is a config bug in the AGENT (model id, API key, deprecated "
        f"params) — not the harness. Fix the agent and re-run.{hint}",
        title="Evaluation aborted", border_style="red",
    ))
    raise typer.Exit(code=2)


@app.command("run")
def run(
    agent_file: Path = typer.Argument(..., exists=True, help="Python file exposing a callable named `agent`."),
    entry: str = typer.Option("agent", "--entry", help="Name of the callable inside the file."),
    role: str = typer.Option("an AI agent", "--role"),
    business_case: str = typer.Option("", "--business-case"),
    goal: str = typer.Option("", "--goal"),
    turns: int = typer.Option(15, "--turns", min=1, max=50),
    consensus: str = typer.Option("delphi", "--consensus", help="independent | delphi | debate"),
    seed: int | None = typer.Option(
        None, "--seed",
        help="Deterministic scoring for reproducible runs (OpenAI / Gemini honor it; "
             "Anthropic does not yet)."),
    metrics: str | None = typer.Option(None, "--metrics", help="Comma-separated metric names."),
    extra_traps: str | None = typer.Option(
        None, "--extra-traps",
        help="Comma-separated paths to custom trap .md files or dirs (client report B5)."),
    trap_packs: str | None = typer.Option(
        None, "--trap-packs", help="Comma-separated installed trap-pack names."),
    pin_traps: str | None = typer.Option(
        None, "--pin-traps",
        help="Comma-separated trap NAMES to FORCE into the plan regardless of "
             "selection scoring (client report B2 - pin a custom trap that would "
             "otherwise lose to domain-matched traps)."),
    context_dir: Path | None = typer.Option(
        None, "--context-dir",
        exists=True, file_okay=False, dir_okay=True,
        help="Directory that DEFINES THE AGENT, loaded via AgentContext.from_dir(): "
             "system_prompt.md (or .txt), tools.json, memory.jsonl, and an optional agent.yaml "
             "manifest (role / goal / business-case). Lifts the limited-context ceilings on "
             "instruction_following / safety. Explicit CLI flags override the manifest."),
    domain_knowledge_dir: Path | None = typer.Option(
        None, "--domain-knowledge-dir",
        exists=True,
        help="Directory of DOMAIN KNOWLEDGE the agent is grounded on (policies, specs, FAQs - "
             "any .md / .txt / .json / .yaml). A SEPARATE input from --context-dir. Used for "
             "hallucination-resistance scoring."),
    llm: str | None = typer.Option(None, "--llm", help="Model id (LiteLLM target)."),
    fallback_llm: str | None = typer.Option(
        None, "--fallback-llm",
        help="Optional secondary Harness LLM that rescues failed primary calls "
             "(rate-limit / quota / empty / bad JSON). Cross-family is best, e.g. "
             "--llm gpt-4.1-mini --fallback-llm anthropic/claude-haiku-4-5. "
             "Defaults to env PROOFAGENT_FALLBACK_LLM.",
    ),
    json_out: Path | None = typer.Option(None, "--json", help="Write report JSON to this path."),
    md_out: Path | None = typer.Option(None, "--markdown", help="Write report Markdown to this path."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress live progress UI."),
    assess_context: bool = typer.Option(
        False, "--assess-context",
        help="Also grade the QUALITY of the agent's context (system prompt + "
             "tool schemas) as a SEPARATE sub-score. Off by default; never "
             "affects the metric scores, certification, or the gate.",
    ),
    assess_compliance: bool = typer.Option(
        False, "--assess-compliance",
        help="After the jury, map the run to the SELECTED regulatory frameworks "
             "(status + why-not-compliant / proof / fix per control), using the "
             "jury's findings as evidence. One extra harness-LLM call covering all "
             "selected frameworks. "
             "Off by default; never affects the metric scores, certification, or the gate.",
    ),
    frameworks: str | None = typer.Option(
        None, "--frameworks",
        help="Comma-separated framework ids to assess with --assess-compliance "
             "(e.g. eu_ai_act,soc2,iso_42001). Defaults to the profile's selection "
             "(or the core set). Ignored unless --assess-compliance is set.",
    ),
    # ── Governance upload (gate CI/CD on the release decision) ──
    # OFF by default - a vanilla `proof run` stays fully local, no network.
    upload: bool = typer.Option(
        False, "--upload/--no-upload",
        help="Upload the result to the ProofAgent Governance API and gate on "
             "the returned decision (exit 0 pass · 2 block; review exits 1 "
             "only with --fail-on review, else 0).",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key",
        help="API key for the Governance API. Defaults to env PROOFAGENT_API_KEY. "
             "Get a key at https://app.proofagent.ai -> Settings -> API Keys.",
    ),
    agent: str | None = typer.Option(
        None, "--agent",
        help="Name of the agent under test - this is the name shown on the governance "
             "dashboard, and it groups runs + regressions together. Defaults to --role.",
    ),
    agent_version: str | None = typer.Option(
        None, "--agent-version", help="Version / git ref of the agent under test."),
    profile: str | None = typer.Option(
        None, "--profile",
        help="Governance profile slug to evaluate against (e.g. "
             "airline_customer_support)."),
    governance_profile: Path | None = typer.Option(
        None, "--governance-profile",
        help="Agent Governance Profile as CODE — a local YAML/JSON risk-classification "
             "file (same shape as the dashboard's). The harness classifies it, steers "
             "trap selection + the context bar to that risk tier, scopes compliance, and "
             "gates the release LOCALLY (tier guardrails → pass/review/block). Works fully "
             "offline; with --upload it also stores the classification on the agent."),
    assess_governance: bool = typer.Option(
        False, "--assess-governance",
        help="Pull the Agent Governance Profile bound to --agent FROM the cloud (by name) "
             "and gate against it. Needs an API key. Ignored if --governance-profile is set."),
    fail_on: str | None = typer.Option(
        None, "--fail-on",
        help="Which gate decision fails the build: pass | review | block. "
             "Default: the attached governance profile's fail_on when present, "
             "else 'block' (review is informational)."),
    source: str = typer.Option(
        "ci_cd", "--source",
        help="Origin of this run: local | ci_cd | manual | api | scheduled."),
    environment: str | None = typer.Option(
        None, "--environment", "--env",
        help="Deployment environment: development | staging | production. Recorded on "
             "the run; governance uses it for release decisions + workflow matching."),
) -> None:
    """Run the harness against an agent defined in a Python file."""
    callable_obj = _load_callable(agent_file, entry)
    metric_list = (
        [m.strip() for m in metrics.split(",") if m.strip()]
        if metrics
        else list(CANONICAL_METRICS)
    )

    def _csv(s: str | None) -> list[str] | None:
        return [x.strip() for x in s.split(",") if x.strip()] if s else None

    harness = Harness(
        llm=llm,
        fallback_llm=fallback_llm or os.environ.get("PROOFAGENT_FALLBACK_LLM"),
        metrics=metric_list,
        turns=turns,
        consensus=consensus,
        seed=seed,
        # Optional panel-size control: PROOFAGENT_PERSONAS="rigorous,lenient"
        # runs a smaller jury (fewer LLM calls) for cost/latency-bound runs.
        # Unset → the default 3-persona panel (unchanged behavior).
        personas=_csv(os.environ.get("PROOFAGENT_PERSONAS")),
        extra_traps=_csv(extra_traps),
        trap_packs=_csv(trap_packs),
        pin_traps=_csv(pin_traps),
        verbose=not quiet,
    )

    # Load the full agent context from a directory when --context-dir is given.
    # This passes system_prompt + tools + knowledge + memory to the harness (no
    # limited-context ceilings) and lets role/goal/business-case live in the
    # context's manifest. Explicit CLI flags always win over the manifest.
    ctx = AgentContext.from_dir(str(context_dir)) if context_dir else None
    eff_role = role if role != "an AI agent" else (ctx.role if ctx and ctx.role else role)
    eff_goal = goal or (ctx.goal if ctx and ctx.goal else goal)
    eff_business = business_case or (ctx.business_case if ctx and ctx.business_case else business_case)

    # Agent Governance Profile (governance as code) — optional. Attaching it makes
    # the run's risk classification steer trap selection + the context-engineering
    # bar (via harness.governance_profile → graph state), auto-scope compliance to
    # its frameworks, and drive the LOCAL gate. Resolved BEFORE compliance so its
    # frameworks-in-scope can auto-scope --assess-compliance.
    gov_profile = _resolve_governance_profile(
        file=governance_profile,
        assess=assess_governance,
        agent=agent or eff_role,
        api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"),
        api_base=os.environ.get("PROOFAGENT_API_BASE_URL"),
    )
    harness.governance_profile = gov_profile

    # --fail-on precedence: explicit flag > the profile's declared fail_on > block.
    eff_fail_on = (fail_on or (gov_profile.fail_on if gov_profile else None) or "block").lower()
    if eff_fail_on not in ("pass", "review", "block"):
        console.print(f"[red]--fail-on must be pass | review | block (got {eff_fail_on!r}).[/red]")
        raise typer.Exit(code=2)

    # Compliance scope: --frameworks wins; else the attached governance profile's
    # frameworks-in-scope; else --assess-compliance with a key fetches the platform
    # selection; else the local default core set (pure OSS).
    compliance_fw, compliance_src = _resolve_compliance_frameworks(
        enabled=assess_compliance,
        explicit=_csv(frameworks),
        api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"),
        api_base=os.environ.get("PROOFAGENT_API_BASE_URL"),
        profile=profile,
        agent=agent or eff_role,
        governance_frameworks=(gov_profile.compliance_framework_ids() if gov_profile else None),
    )

    if not quiet:
        cfg = [
            ("Agent file", f"{agent_file}  (entry: {entry})"),
            ("Harness LLM", llm or "(default)"),
            ("Fallback LLM", fallback_llm or os.environ.get("PROOFAGENT_FALLBACK_LLM") or "-"),
            ("Turns", str(turns)),
            ("Consensus", consensus),
            ("Seed", str(seed) if seed is not None else "-"),
            ("Metrics", ", ".join(metric_list)),
            ("Context dir", str(context_dir) if context_dir else "-"),
            ("Domain knowledge", str(domain_knowledge_dir) if domain_knowledge_dir else "-"),
            ("Assess context", "yes" if assess_context else "no"),
            ("Assess compliance", f"yes · {compliance_src}" if assess_compliance else "no"),
            ("Governance profile", f"{gov_profile.tier_label} · {gov_profile.name}" if gov_profile else "-"),
            ("Role", eff_role),
            ("Upload", "yes  →  app.proofagent.ai" if upload else "no (local only)"),
        ]
        if upload:
            cfg += [
                ("Agent (dashboard)", agent or eff_role),
                ("Agent version", agent_version or "-"),
                ("Gate profile", profile or "-"),
                ("Fail on", eff_fail_on),
            ]
        _print_run_config("multi-turn", cfg)
        if gov_profile is not None:
            _print_governance_card(gov_profile)

    from proofagent_harness.agents.conductor import AgentUnderTestError
    try:
        report = harness.evaluate(
            callable_obj,
            role=eff_role,
            business_case=eff_business,
            goal=eff_goal,
            context=ctx,          # the agent: system_prompt + tools + memory
            knowledge=str(domain_knowledge_dir) if domain_knowledge_dir else None,
            assess_context=assess_context,
            assess_compliance=assess_compliance,
            compliance_frameworks=compliance_fw,
        )
    except AgentUnderTestError as exc:
        _abort_on_agent_crash(exc)

    _print_context_engineering(report)

    if json_out:
        report.to_json(str(json_out))
        console.print(f"[dim]Report JSON written to {json_out}[/dim]")
    if md_out:
        report.to_markdown(str(md_out))
        console.print(f"[dim]Report Markdown written to {md_out}[/dim]")

    # Agent Governance Profile gate is AUTHORITATIVE when a profile is attached —
    # the tier guardrails decide pass/review/block locally (and the run + profile
    # upload for the dashboard when --upload). Always raises typer.Exit.
    if gov_profile is not None:
        _governance_gate_and_exit(
            report,
            gov_profile,
            fail_on=eff_fail_on,
            upload=upload,
            api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"),
            agent_name=agent or eff_role,
            agent_version=agent_version,
            profile_slug=profile,
            source=source,
            environment=environment,
            transcript=_transcript_text(report),
        )

    if upload:
        _upload_and_gate(
            report,
            api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"),
            agent_name=agent or eff_role,
            agent_version=agent_version,
            profile=profile,
            fail_on=eff_fail_on,
            source=source,
            environment=environment,
            transcript=_transcript_text(report),
        )
        # _upload_and_gate always raises typer.Exit with the gate code.

    raise typer.Exit(code=0 if report.certification.value != "NOT_READY" else 1)


@app.command("artifact")
def artifact(
    artifact_path: Path = typer.Argument(
        ..., exists=True, help="Deliverable to score (.md/.txt/.pdf/.docx/.html/.png)."
    ),
    knowledge_dir: Path | None = typer.Option(
        None, "--domain-knowledge-dir", "--knowledge-dir", "-k",
        help="Folder of DOMAIN KNOWLEDGE / ground-truth docs to grade the artifact against "
             "(policies, specs, source data - .md / .txt / .json / .yaml). "
             "(--knowledge-dir is a back-compat alias.)"
    ),
    artifact_type: str = typer.Option(
        "BRD", "--type", "-t", help="Artifact type: BRD | report | code | business_plan | ..."
    ),
    role: str = typer.Option("an AI agent producing a deliverable", "--role"),
    business_case: str = typer.Option("", "--business-case"),
    llm: str | None = typer.Option(None, "--llm", help="Harness LLM (LiteLLM target)."),
    fallback_llm: str | None = typer.Option(
        None, "--fallback-llm", help="Cross-family fallback LLM. Defaults to env PROOFAGENT_FALLBACK_LLM."
    ),
    consensus: str = typer.Option("delphi", "--consensus", help="independent | delphi | debate"),
    seed: int = typer.Option(42, "--seed"),
    json_out: Path | None = typer.Option(None, "--json", help="Write report JSON to this path."),
    md_out: Path | None = typer.Option(None, "--markdown", help="Write report Markdown to this path."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress live progress UI."),
    assess_context: bool = typer.Option(
        False, "--assess-context",
        help="Also grade the QUALITY of the producing agent's context "
             "(auto-bundled agent_system_prompt.md / agent_tools.json) as a "
             "SEPARATE sub-score. Off by default; never affects the score or gate.",
    ),
    assess_compliance: bool = typer.Option(
        False, "--assess-compliance",
        help="After the jury, map the artifact to the SELECTED regulatory frameworks "
             "(status + why/proof/fix per control). One extra harness-LLM call per "
             "framework. Off by default; never affects the score, certification, or gate.",
    ),
    frameworks: str | None = typer.Option(
        None, "--frameworks",
        help="Comma-separated framework ids for --assess-compliance "
             "(e.g. eu_ai_act,soc2). Defaults to the profile selection / core set.",
    ),
    upload: bool = typer.Option(
        False, "--upload/--no-upload",
        help="Upload to the Governance API and gate on the decision (exit 0/1/2).",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key",
        help="API key for the Governance API. Defaults to env PROOFAGENT_API_KEY. "
             "Get a key at https://app.proofagent.ai -> Settings -> API Keys."
    ),
    agent: str | None = typer.Option(
        None, "--agent",
        help="Name of the agent shown on the governance dashboard (groups runs). "
             "Defaults to --role."
    ),
    agent_version: str | None = typer.Option(None, "--agent-version"),
    profile: str | None = typer.Option(
        None, "--profile", help="Governance profile slug (e.g. artifact_governance_default)."
    ),
    fail_on: str = typer.Option("block", "--fail-on", help="pass | review | block."),
    source: str = typer.Option("ci_cd", "--source"),
    environment: str | None = typer.Option(
        None, "--environment", "--env",
        help="Deployment environment: development | staging | production."),
) -> None:
    """Run an ARTIFACT-mode evaluation: score a finished deliverable (no conversation)."""
    import json as _json

    from proofagent_harness import AgentArtifact, AgentContext, KnowledgeCorpus

    art = AgentArtifact.from_path(artifact_path, type=artifact_type)
    corpus = (
        KnowledgeCorpus(sources=[str(knowledge_dir)], extensions=[".md", ".txt", ".json", ".yaml", ".yml"], max_chars=200_000)
        if knowledge_dir and Path(knowledge_dir).exists()
        else None
    )
    # Auto-bundle the producing agent's contract when sibling files exist.
    bundle = artifact_path.parent
    sp, tp, tr = bundle / "agent_system_prompt.md", bundle / "agent_tools.json", bundle / "agent_trace.md"
    system_prompt = sp.read_text() if sp.exists() else None
    tools = _json.loads(tp.read_text()) if tp.exists() else None
    agent_trace = tr.read_text() if tr.exists() else None
    context = (
        AgentContext(system_prompt=system_prompt, tools=tools or [])
        if (system_prompt or tools)
        else None
    )

    # Compliance scope: --frameworks wins; else --assess-compliance with a key fetches
    # the governance profile's selection; else the local default core set (pure OSS).
    compliance_fw, compliance_src = _resolve_compliance_frameworks(
        enabled=assess_compliance,
        explicit=[x.strip() for x in frameworks.split(",") if x.strip()] if frameworks else None,
        api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"),
        api_base=os.environ.get("PROOFAGENT_API_BASE_URL"),
        profile=profile,
        agent=agent or role,
    )

    if not quiet:
        cfg = [
            ("Artifact", f"{artifact_path}  (type: {artifact_type})"),
            ("Harness LLM", llm or "(default)"),
            ("Fallback LLM", fallback_llm or os.environ.get("PROOFAGENT_FALLBACK_LLM") or "-"),
            ("Consensus", consensus),
            ("Seed", str(seed)),
            ("Domain knowledge", str(knowledge_dir) if knowledge_dir else "-"),
            ("Assess context", "yes" if assess_context else "no"),
            ("Assess compliance", f"yes · {compliance_src}" if assess_compliance else "no"),
            ("Role", role),
            ("Upload", "yes  →  app.proofagent.ai" if upload else "no (local only)"),
        ]
        if upload:
            cfg += [
                ("Agent (dashboard)", agent or role),
                ("Agent version", agent_version or "-"),
                ("Gate profile", profile or "-"),
                ("Fail on", fail_on),
            ]
        _print_run_config("artifact", cfg)

    from proofagent_harness.agents.conductor import AgentUnderTestError
    try:
        report = Harness(
            mode="artifact",
            llm=llm,
            fallback_llm=fallback_llm or os.environ.get("PROOFAGENT_FALLBACK_LLM"),
            consensus=consensus,
            seed=seed,
            verbose=not quiet,
        ).evaluate(
            artifact=art,
            knowledge_corpus=corpus,
            role=role,
            business_case=business_case,
            context=context,
            agent_trace=agent_trace,
            assess_context=assess_context,
            assess_compliance=assess_compliance,
            compliance_frameworks=compliance_fw,
        )
    except AgentUnderTestError as exc:
        _abort_on_agent_crash(exc)

    _print_context_engineering(report)

    if json_out:
        report.to_json(str(json_out))
        console.print(f"[dim]Report JSON written to {json_out}[/dim]")
    if md_out:
        report.to_markdown(str(md_out))
        console.print(f"[dim]Report Markdown written to {md_out}[/dim]")

    if upload:
        _upload_and_gate(
            report,
            api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"),
            agent_name=agent or role,
            agent_version=agent_version,
            profile=profile,
            fail_on=fail_on,
            source=source,
            environment=environment,
            artifact_text=getattr(art, "generated_artifact", None),
            knowledge_text=(
                "\n\n".join(
                    p.read_text(errors="ignore")
                    for p in sorted(Path(knowledge_dir).rglob("*.md"))
                )[:24000]
                if knowledge_dir and Path(knowledge_dir).exists()
                else None
            ),
        )

    raise typer.Exit(code=0 if report.certification.value != "NOT_READY" else 1)


@app.command("session")
def session(
    source: Path | None = typer.Argument(
        None,
        help="OPTIONAL override: a normalized events .jsonl or a coding-tool "
             "transcript. Omit it and the harness auto-discovers the session "
             "(Claude Code transcript for --workspace, else the workspace git diff).",
    ),
    tool: str = typer.Option(
        "auto", "--tool",
        help="Coding tool: auto (detect) | claude-code | cursor | copilot | windsurf | generic."),
    workspace: Path = typer.Option(
        Path("."), "--workspace",
        help="Repo root the session governs (used by --from-git + scope checks)."),
    from_git: bool = typer.Option(
        False, "--from-git",
        help="Capture write events from the workspace `git diff` (tool-agnostic)."),
    scope: str | None = typer.Option(
        None, "--scope", help="Comma-separated in-scope path globs (blast-radius policy)."),
    deny: str | None = typer.Option(
        None, "--deny",
        help="Comma-separated never-touch path globs, e.g. **/.env,**/secrets/**."),
    screen: str | None = typer.Option(
        None, "--screen",
        help="Comma-separated Tier-1 screens (default all): "
             "secrets,pii,dangerous-cmd,egress,scope,deps,license,cwe."),
    # ── Tier-2 escalation (cost-gated deep evaluation) ──
    assess: str = typer.Option(
        "auto", "--assess",
        help="Tier-2 (LLM artifact rubric) policy: auto (only when Tier-1 finds a "
             "critical - the default, low-cost), never (Tier-1 only, always 0 tokens), "
             "always (force a deep audit)."),
    escalate_on: str = typer.Option(
        "critical", "--escalate-on",
        help="Severity bar that triggers Tier-2 under --assess auto: critical | high."),
    llm: str | None = typer.Option(
        None, "--llm",
        help="Harness LLM for the Tier-2 rubric (LiteLLM target). "
             "Defaults to env PROOFAGENT_LLM."),
    narrate: bool = typer.Option(
        False, "--narrate",
        help="Enrich the intent trajectory with ONE batched LLM call over the whole "
             "session: each turn gets a crisp intent + a one-line summary of what the "
             "agent did. Uses --llm / PROOFAGENT_LLM. Off = deterministic trajectory "
             "(raw prompt preview, 0 tokens)."),
    # ── Governance block - identical to `run` / `artifact` ──
    upload: bool = typer.Option(
        False, "--upload/--no-upload",
        help="Upload to the Governance API and gate on the decision (exit 0/1/2)."),
    api_key: str | None = typer.Option(
        None, "--api-key",
        help="API key for the Governance API. Defaults to env PROOFAGENT_API_KEY."),
    agent: str | None = typer.Option(
        None, "--agent",
        help="Agent name shown on the governance dashboard (groups sessions)."),
    agent_version: str | None = typer.Option(None, "--agent-version"),
    profile: str | None = typer.Option(
        None, "--profile", help="Governance profile slug (policy-as-code)."),
    fail_on: str = typer.Option("block", "--fail-on", help="pass | review | block."),
    source_kind: str = typer.Option(
        "local", "--source", help="local | ci_cd | manual | api | scheduled."),
    environment: str | None = typer.Option(
        None, "--environment", "--env",
        help="Deployment environment: development | staging | production."),
    json_out: Path | None = typer.Option(None, "--json", help="Write the session payload JSON here."),
    md_out: Path | None = typer.Option(None, "--markdown", help="Write a Markdown summary here."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress the scorecard."),
) -> None:
    """Govern a live AI coding-agent SESSION (Claude Code, Cursor, Copilot, Windsurf).

    Tier-1 deterministic screening (0 tokens) over the session's captured events
    (tool calls, edits, commands, diffs) -> findings + a risk score, uploaded to
    the dashboard as a ``mode=session`` run. Additive: does not touch multi-turn /
    artifact.
    """
    from proofagent_harness.session import SessionRunner, build_session_payload
    from proofagent_harness.session.adapters import (
        discover_transcript_path,
        load_session,
        session_key_for,
    )
    from proofagent_harness.session.escalate import assess_critical_slice
    from proofagent_harness.session.screen import SCREENS, ScreenContext
    from proofagent_harness.session.tools import display_name as _tool_name

    def _csv(s: str | None) -> list[str] | None:
        return [x.strip() for x in s.split(",") if x.strip()] if s else None

    # Auto-discover the session (Claude Code transcript / workspace git diff)
    # unless an explicit source or --from-git is given. `tool_norm` is what was
    # actually found, so the scorecard + payload label the real tool.
    events, tool_norm = load_session(
        source=str(source) if source else None,
        tool=tool, workspace=str(workspace), from_git_flag=from_git,
    )
    # Stable session identity so re-running `proof session` on the SAME transcript
    # updates its run in place instead of piling up duplicates (upsert on the API).
    _sess_path = source if source else discover_transcript_path(str(workspace))
    session_key = session_key_for(_sess_path)
    if not events:
        console.print(
            "[yellow]No session found. The harness looked for a Claude Code transcript "
            f"for {workspace} and a git diff there. Pass an events .jsonl / transcript, "
            "or use --from-git in a repo with changes.[/yellow]")
        raise typer.Exit(code=1)

    screens = _csv(screen) or list(SCREENS)
    ctx = ScreenContext(scope=_csv(scope) or [], deny=_csv(deny) or [])
    caps = {
        "tool": tool_norm, "workspace": str(workspace),
        "scope": _csv(scope) or [], "deny": _csv(deny) or [],
    }
    result = SessionRunner(screens=_csv(screen), ctx=ctx).run(events, capabilities=caps)

    # Tier-2: cost-gated deep evaluation. Off unless Tier-1 hit the escalation bar
    # (default: a critical). Only the flagged critical code slice is graded.
    if assess != "never" and (assess == "always" or any(
        f.severity in ("critical",) if escalate_on == "critical"
        else f.severity in ("critical", "high") for f in result.findings
    )):
        console.print(
            f"[dim]Tier-1 hit the {escalate_on} bar -> escalating the critical code "
            f"slice to Tier-2 (artifact rubric, {llm or os.environ.get('PROOFAGENT_LLM') or 'default LLM'}) …[/dim]")
    outcome = assess_critical_slice(
        events, result.findings, mode=assess, on=escalate_on,
        llm=llm or os.environ.get("PROOFAGENT_LLM"),
    )
    result.tier2 = {**outcome.as_dict(), "findings_payload": outcome.findings}

    if not quiet:
        _session_scorecard(result, tool_norm, screens, upload)

    payload = build_session_payload(
        result, agent_name=agent or _tool_name(tool_norm),
        agent_version=agent_version, profile=profile, source=source_kind,
        environment=environment, tool=tool_norm, screens=screens,
        narrate=narrate, llm=llm or os.environ.get("PROOFAGENT_LLM"),
        session_key=session_key,
    )
    if narrate and not (llm or os.environ.get("PROOFAGENT_LLM")):
        console.print(
            "[yellow]--narrate needs an LLM (--llm or PROOFAGENT_LLM); "
            "using the deterministic trajectory (raw prompts, 0 tokens).[/yellow]")

    if json_out:
        import json as _json
        json_out.write_text(_json.dumps(payload, indent=2))
        console.print(f"[dim]Session JSON written to {json_out}[/dim]")
    if md_out:
        md_out.write_text(_session_markdown(result, tool_norm))
        console.print(f"[dim]Session Markdown written to {md_out}[/dim]")

    if upload:
        _upload_session_and_gate(
            payload, api_key=api_key or os.environ.get("PROOFAGENT_API_KEY"), fail_on=fail_on)
        # _upload_session_and_gate always raises typer.Exit with the gate code.

    raise typer.Exit(code=0 if result.certification != "NOT_READY" else 1)


@app.command("watch")
def watch(
    workspace: str | None = typer.Option(
        None, "--workspace",
        help="Repo to watch. OMIT IT to auto-attach to the most recently active Claude "
             "Code session anywhere — no path, no cd needed. Pass a path only to pin a "
             "specific repo."),
    tool: str = typer.Option("auto", "--tool", help="auto (detect) | claude-code | cursor | ..."),
    interval: int = typer.Option(
        120, "--interval", "--every",
        help="Seconds between each ProofAgent evaluation + upload (same unit as --screen-every). "
             "Tier-1 screening runs continuously; this is the cadence for the uploaded step."),
    screen_every: int = typer.Option(
        30, "--screen-every",
        help="Seconds between Tier-1 screens (0 tokens). The local session is refreshed "
             "this often; the ProofAgent evaluation + upload still fire only every --interval seconds."),
    state_dir: str | None = typer.Option(
        None, "--state-dir",
        help="Where the durable local session file lives (default ~/.proofagent/live). "
             "Only the synthesis is uploaded; this file never leaves the machine."),
    once: bool = typer.Option(
        False, "--once", help="Scan once and exit (no loop) — for CI / a single snapshot."),
    scope: str | None = typer.Option(
        None, "--scope", help="Comma-separated in-scope path globs (blast-radius policy)."),
    deny: str | None = typer.Option(
        None, "--deny", help="Comma-separated never-touch path globs, e.g. **/.env,**/secrets/**."),
    screen: str | None = typer.Option(
        None, "--screen", help="Comma-separated Tier-1 screens (default all)."),
    escalate_on: str = typer.Option(
        "high", "--escalate-on",
        help="Tier-1 severity that escalates to Tier-2 (the artifact code rubric) and the "
             "harness-LLM trajectory: critical | high."),
    assess: str = typer.Option(
        "auto", "--assess",
        help="Tier-2 deep code rubric: auto (grade the flagged slice on the escalation bar), "
             "never (Tier-1 only, always 0 tokens), always (grade every scan). Re-graded only "
             "when the flagged slice changes, so a live loop never re-spends on the same code."),
    analyze_every_interval: bool = typer.Option(
        False, "--analyze-every-interval",
        help="Run the harness-LLM trajectory analysis on EVERY interval that has new turns, "
             "instead of only on a new risk / a batch of new intents / a periodic cap. Labels the "
             "newest turn within one interval, at the cost of more LLM calls (ceiling 3600/interval "
             "per hour; 0 while idle). Default: the signal-driven schedule."),
    llm: str | None = typer.Option(
        None, "--llm",
        help="Harness LLM for Tier-2 + the risk-driven trajectory (LiteLLM target). "
             "Defaults to env PROOFAGENT_LLM. Without it, Tier-2 stays dormant and the "
             "deterministic trajectory is used (Tier-1 gating is unaffected)."),
    upload: bool = typer.Option(
        True, "--upload/--no-upload",
        help="Upsert the growing live session to the Governance dashboard each scan."),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Governance API key. Defaults to env PROOFAGENT_API_KEY."),
    agent: str | None = typer.Option(
        None, "--agent", help="Coding-agent name on the dashboard (groups a repo's sessions)."),
    profile: str | None = typer.Option(None, "--profile", help="Governance profile slug."),
    source_kind: str = typer.Option("local", "--source"),
    environment: str | None = typer.Option(None, "--environment", "--env"),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write the latest scan's payload JSON here (debug/CI)."),
) -> None:
    """Watch a LIVE AI coding session and stream its RISK-DRIVEN INTENT TRAJECTORY to
    the dashboard, in near-real-time.

    Every ``--interval`` seconds: Tier-1 deterministically screens the whole session so
    far (0 tokens). If a risk at/above ``--escalate-on`` is present AND an LLM is
    configured (``--llm``/PROOFAGENT_LLM), it escalates to Tier-2 — the artifact code
    rubric grades the flagged critical slice (``--assess``; re-graded only when that
    slice changes, so the loop never re-spends) — and one batched call builds the
    risk-driven trajectory (intents + what the agent did + risk). Without an LLM,
    Tier-2 + trajectory stay dormant; Tier-1 screening/gating is unaffected. The growing
    session is upserted as a SINGLE live run (stable ``session_key``), so the dashboard
    fills in as you work. Ctrl-C flushes a final ``live=false`` snapshot. Observe-only —
    never gates the agent.
    """
    import time
    from datetime import datetime, timedelta, timezone

    from proofagent_harness.governance import (
        DEFAULT_API_BASE_URL,
        GovernanceUploadError,
        upload_run,
    )
    from proofagent_harness.session import SessionRunner, build_session_payload
    from proofagent_harness.session.adapters import (
        discover_latest_transcript,
        discover_transcript_path,
        load_session,
        session_key_for,
        workspace_of_transcript,
    )
    from proofagent_harness.session.cursor import (
        cursor_session_key,
        discover_cursor_db,
        from_cursor,
    )
    from proofagent_harness.session.escalate import assess_critical_slice, escalating_seqs
    from proofagent_harness.session.live import LiveSession
    from proofagent_harness.session.screen import SCREENS, ScreenContext
    from proofagent_harness.session.tools import display_name as _tool_name

    def _csv(s: str | None) -> list[str] | None:
        return [x.strip() for x in s.split(",") if x.strip()] if s else None

    llm_target = llm or os.environ.get("PROOFAGENT_LLM")
    key = api_key or os.environ.get("PROOFAGENT_API_KEY")
    api_url = os.environ.get("PROOFAGENT_API_BASE_URL", "").rstrip("/") or DEFAULT_API_BASE_URL
    if upload and not key:
        console.print(
            "[red]--upload needs an API key (--api-key or PROOFAGENT_API_KEY). "
            "Use --no-upload to watch locally.[/red]")
        raise typer.Exit(code=2)

    screens = _csv(screen) or list(SCREENS)
    ctx = ScreenContext(scope=_csv(scope) or [], deny=_csv(deny) or [])
    bar = ("critical",) if escalate_on == "critical" else ("critical", "high")
    # WHICH session to watch. With --workspace we discover that repo's session; with NO
    # --workspace we auto-attach to the most recently active Claude Code session ANYWHERE
    # (the zero-path default). Cursor sessions come from its per-workspace SQLite DB. Any
    # other editor falls back to the workspace git diff. Re-resolved every scan.
    global_mode = workspace is None and tool in ("auto", "claude-code")

    def resolve_src() -> tuple[Path | None, str, str]:
        """(source_path_or_None, workspace, kind) — kind ∈ {'claude','cursor','git'}."""
        if tool == "cursor":
            return discover_cursor_db(workspace or "."), (workspace or "."), "cursor"
        if tool in ("auto", "claude-code"):
            p = discover_transcript_path(workspace) if workspace else discover_latest_transcript()
            if p is not None:
                return p, (workspace or workspace_of_transcript(p) or "."), "claude"
            if tool == "auto":  # no Claude session — try Cursor's DB, else the git diff
                db = discover_cursor_db(workspace or ".")
                if db is not None:
                    return db, (workspace or "."), "cursor"
        return None, (workspace or "."), "git"

    _path0, _ws0, _kind0 = resolve_src()
    ws_name = Path(_ws0).resolve().name or "session"

    # Make discovery VISIBLE — the source is DISCOVERED, not configured.
    if _kind0 == "claude" and _path0 is not None:
        _where = "most recently active · " if global_mode else ""
        watching = (f"{_where}Claude Code session [cyan]{session_key_for(_path0)}[/cyan] "
                    f"[dim]({Path(_ws0).name} — {_path0})[/dim]")
    elif _kind0 == "cursor" and _path0 is not None:
        watching = (f"Cursor session [cyan]{cursor_session_key(_path0)}[/cyan] "
                    f"[dim]({ws_name} — {_path0})[/dim]")
    elif tool == "cursor":
        watching = (f"[yellow]no Cursor workspace for {ws_name} yet[/yellow] — "
                    f"open this repo in Cursor and chat")
    elif tool in ("auto", "claude-code"):
        watching = (
            "[yellow]no active Claude Code session found yet[/yellow] — "
            "start coding in any repo and it attaches automatically"
            if global_mode else
            f"[yellow]no Claude Code transcript for {ws_name} yet[/yellow] — "
            f"start coding here (or edit files and it reads the git diff)")
    else:
        watching = f"git working tree of [cyan]{ws_name}[/cyan]"

    if assess == "never":
        t2_desc = "[dim]Tier-2 off (--assess never)[/dim]"
    elif not llm_target:
        t2_desc = f"Tier-2 armed on [yellow]{escalate_on}[/yellow] [dim](set --llm to run it)[/dim]"
    else:
        t2_desc = f"Tier-2 on [yellow]{escalate_on}[/yellow] [dim](LLM {llm_target})[/dim]"
    # Which agent this session reports to on the portal (attribution is by name).
    _btool = ("claude-code" if _kind0 == "claude" else "cursor" if _kind0 == "cursor"
              else (tool if tool != "auto" else "generic"))
    report_agent = agent or _tool_name(_btool)
    console.print(
        f"[bold]proof watch[/bold] · {ws_name} · every {interval}s · {t2_desc} · "
        f"{'uploading' if upload else 'local only'}\n"
        f"[dim]watching:[/dim] {watching}\n"
        f"[dim]reports to agent:[/dim] [cyan]{report_agent}[/cyan]\n[dim]Ctrl-C to stop.[/dim]")

    # ── Preflight: check every moving part BEFORE the loop so a bad key/model or a missing
    # session surfaces up front, not silently mid-run. Non-fatal — the watch still starts.
    _tool_label = {"claude": "Claude Code", "cursor": "Cursor"}.get(_kind0, "git working tree")
    if _path0 is not None or _kind0 == "git":
        console.print(f"[green]  ✓[/green] connected to {_tool_label} · [cyan]{ws_name}[/cyan]")
    else:
        console.print(f"[yellow]  ✗ no {_tool_label} session found yet — will attach when one appears[/yellow]")
    if llm_target:
        from proofagent_harness.session.narrate import probe_llm
        _ok, _err = probe_llm(llm_target)
        if _ok:
            console.print(f"[green]  ✓[/green] harness LLM reachable · [cyan]{llm_target}[/cyan]")
        else:
            console.print(
                f"[yellow]  ✗ harness LLM NOT reachable · {llm_target} — {_err}[/yellow]\n"
                "[yellow]      → intents stay the deterministic (approx) labels + 0 eval tokens "
                "until the key/model is fixed IN THIS SHELL[/yellow]")
    else:
        console.print("[dim]  · no --llm — deterministic trajectory (0 eval tokens)[/dim]")
    if upload:
        console.print(f"[green]  ✓[/green] uploading to [cyan]{api_url}[/cyan]")

    # Tier-2 is cost-gated across scans (cache the last-graded slice); the local session
    # accumulator is held here and persisted to disk every tick.
    t2_state: dict[str, object] = {"sig": None, "outcome": None}
    live_state: dict[str, object] = {"sess": None, "shown_url": False}
    # Signal-driven LLM flush: the trajectory synthesis fires only when something new
    # warrants it (a new risk, a batch of new intents, or a periodic cap) — never per
    # critical, never on a clean tick. Cost tracks risk × activity, not wall-clock.
    flush_state: dict[str, object] = {"risk_sig": None, "intents": 0, "mono": None}
    # Sticky narration: a skip-clean tick uploads the cheap DETERMINISTIC trajectory, which
    # would otherwise overwrite the LLM-narrated intents already on the dashboard. We cache the
    # narrated rows by timestamp and re-apply them on skip ticks, so a turn — once narrated —
    # stays narrated until the next real synthesis refreshes it.
    narr_cache: dict[str, dict[str, str]] = {}
    # Eval tokens are CUMULATIVE narration spend across the rolling session — a skip tick spends
    # 0, so without carrying the running total the run would flash "0 eval tokens" between syntheses.
    eval_state: dict[str, int] = {"tokens": 0}

    def _scan(scan_no: int, *, live: bool, upload_now: bool) -> bool:
        """One tick. Always screens (Tier-1, 0 tokens) and refreshes the durable local
        session. When ``upload_now`` it also runs the paid interval work — Tier-2, the
        LLM trajectory, the slim payload, the upload. Returns True if a session existed."""
        path, ws, kind = resolve_src()
        # Dispatch by discovered source: Cursor's SQLite DB, a Claude Code transcript, or
        # (any other editor) the workspace git diff. Each yields the same SessionEvents.
        if kind == "cursor":
            events = from_cursor(path, workspace=ws) if path is not None else []
            tool_norm, skey = "cursor", (
                cursor_session_key(path) if path is not None
                else f"git:{re.sub(r'[^A-Za-z0-9]', '-', str(Path(ws).resolve()))}")
        elif kind == "claude" and path is not None:
            events, tool_norm = load_session(
                source=str(path), tool="claude-code", workspace=ws, from_git_flag=False)
            skey = session_key_for(path)
        else:
            events, tool_norm = load_session(
                source=None, tool=tool, workspace=ws, from_git_flag=False)
            skey = f"git:{re.sub(r'[^A-Za-z0-9]', '-', str(Path(ws).resolve()))}"
        stamp = datetime.now(timezone.utc).replace(microsecond=0)
        if not events:
            console.print(f"[dim]{stamp:%H:%M:%S} · no live session for {ws_name} yet…[/dim]")
            return False
        result = SessionRunner(screens=_csv(screen), ctx=ctx).run(
            events, capabilities={"tool": tool_norm, "workspace": ws,
                                  "scope": _csv(scope) or [], "deny": _csv(deny) or []},
        )
        counts = result.counts_by_severity
        worst = next((s for s in ("critical", "high", "medium", "low") if counts.get(s)), "")
        risky = any(f.severity in bar for f in result.findings)

        # --- LOCAL session accumulator: refreshed EVERY tick, 0 tokens, never uploaded.
        # This is the durable on-disk source the interval synthesis reads from.
        sess = live_state["sess"]
        if sess is None or getattr(sess, "session_key", "") != skey:
            sess = LiveSession.load_or_new(skey, workspace=ws, tool=tool_norm, state_dir=state_dir)
            live_state["sess"] = sess
        sess.refresh(result, stamp=stamp.isoformat())
        sess.save(state_dir=state_dir)

        if not upload_now:
            # Fast Tier-1 tick — screened + accumulated locally; no LLM, nothing uploaded.
            console.print(
                f"[dim]{stamp:%H:%M:%S} · screen: {len(sess.intents)} intents · "
                f"{counts['critical']}C/{counts['high']}H/{counts['medium']}M · "
                f"peak risk {result.risk_score}/10 · local session · {sess.flagged_count} flagged[/dim]")
            return True

        # --- Signal-driven synthesis: fire ONE LLM trajectory call only when the delta
        # warrants it. skip-clean: no new intents ⇒ no call (deterministic base, 0 tokens,
        # still uploads). Otherwise flush on a NEW risk, a batch of ≥3 new intents, a
        # periodic cap, or the first upload. A single critical never triggers a call by
        # itself; the flush distributes all pending risk across the intents in one shot.
        n_int = len(sess.intents)
        risk_sig = tuple(sorted({(f.category, f.severity) for f in result.findings if f.severity in bar}))
        grew = n_int > int(flush_state["intents"])
        first = flush_state["mono"] is None
        elapsed = time.monotonic() - float(flush_state["mono"] or 0.0)
        should_flush = bool(llm_target) and (first or (grew and (
            analyze_every_interval                                 # --analyze-every-interval: every tick with new turns
            or risk_sig != flush_state["risk_sig"]                 # a new risk surfaced
            or (n_int - int(flush_state["intents"])) >= 3          # ≥3 new intents batched
            or elapsed >= max(1, interval) * 3                     # periodic cap (~3 intervals)
        )))
        narrate_on = should_flush
        if should_flush:
            flush_state.update(risk_sig=risk_sig, intents=n_int, mono=time.monotonic())

        # The deep CODE rubric (Tier-2) is now OPT-IN only via --assess always. It is no
        # longer on the per-critical path — that was the expensive per-escalation call.
        if assess == "always" and llm_target:
            sig = tuple(sorted(escalating_seqs(result.findings, on=escalate_on)))
            if sig != t2_state["sig"]:
                out = assess_critical_slice(
                    events, result.findings, mode=assess, on=escalate_on, llm=llm_target)
                t2_state.update(sig=sig, outcome={**out.as_dict(), "findings_payload": out.findings})
        if t2_state["outcome"]:
            result.tier2 = t2_state["outcome"]

        nxt = (stamp + timedelta(seconds=interval)) if (live and not once) else None
        watch_meta = {
            "live": bool(live and not once),
            "last_scan": stamp.isoformat(),
            "next_scan": nxt.isoformat() if nxt else None,
            "interval_seconds": interval,
            "scans": scan_no,
            "escalate_on": escalate_on,
        }
        payload = build_session_payload(
            # Default agent = the tool's friendly name (e.g. "Claude Code"), so the run
            # attaches to the right coding agent on the portal (attribution is by name).
            # The repo/branch still ride in the BOM; pass --agent to split per-repo.
            result, agent_name=agent or _tool_name(tool_norm),
            profile=profile, source=source_kind, environment=environment,
            tool=tool_norm, screens=screens, narrate=narrate_on, llm=llm_target,
            session_key=skey, watch=watch_meta,
            # Live upload: only the synthesized trajectory + token counters + the
            # risk-bearing events (proof) leave the machine — never the raw firehose.
            live=True,
        )
        # Sticky narration overlay: keep the dashboard from flipping back to the deterministic
        # labels on a skip-clean upload. When we narrated, refresh the cache; when we skipped,
        # re-apply the cached narrated intent/answer/risk_note and keep the run marked narrated.
        _sess_meta = (payload.get("run_metadata") or {}).get("session") or {}
        _traj = _sess_meta.get("trajectory") or []
        if narrate_on:
            for r in _traj:
                ts = r.get("ts")
                if ts and (r.get("answer") or r.get("risk_note")):
                    narr_cache[ts] = {"intent": r.get("intent") or "",
                                      "answer": r.get("answer") or "",
                                      "risk_note": r.get("risk_note") or ""}
        elif narr_cache:
            applied = False
            for r in _traj:
                c = narr_cache.get(r.get("ts") or "")
                if c:
                    if c["intent"]:
                        r["intent"] = c["intent"]
                    if c["answer"]:
                        r["answer"] = c["answer"]
                    if c["risk_note"]:
                        r["risk_note"] = c["risk_note"]
                    applied = True
            if applied:
                _sess_meta["narrated"] = True
        # Carry the CUMULATIVE narration spend so the run's eval-token total doesn't flash 0 on a
        # skip tick (which spends nothing). Add this tick's narration cost, then report the total.
        _tu = payload.get("token_usage") if isinstance(payload.get("token_usage"), dict) else {}
        eval_state["tokens"] += int(_tu.get("narration_tokens", 0) or 0)
        if eval_state["tokens"]:
            # Re-base narration to the running cumulative total WITHOUT clobbering this tick's
            # non-narration spend (e.g. Tier-2 --assess), which build_session_payload folded
            # into total_tokens alongside narration.
            _non_narr = max(0, int(_tu.get("total_tokens", 0) or 0)
                            - int(_tu.get("narration_tokens", 0) or 0))
            payload["token_usage"] = dict(_tu, total_tokens=_non_narr + eval_state["tokens"],
                                          narration_tokens=eval_state["tokens"])
        n_intents = len(sess.intents)
        if not llm_target:
            tier2 = "[green]clean[/green]" if not risky else "[dim]risk — set --llm for trajectory[/dim]"
        elif narrate_on:
            tier2 = "[magenta]Harness synthesis · risk detection[/magenta]"
        else:
            kept = " · kept prior narration" if narr_cache else ""
            tier2 = f"[dim]no new risk/intents — harness synthesis skipped{kept}[/dim]"
        if (result.tier2 or {}).get("status") == "scored":
            tier2 += f" · [magenta]Tier-2 code {result.tier2['final_score']}/10[/magenta]"
        console.print(
            f"{stamp:%H:%M:%S} · eval: {n_intents} intents · "
            f"{counts['critical']}C/{counts['high']}H/{counts['medium']}M · "
            f"worst [bold]{worst or 'none'}[/bold] · peak risk {result.risk_score}/10 · {tier2}")
        _nerr = _sess_meta.get("narration_error")
        if narrate_on and _nerr:
            console.print(
                f"[yellow]  ⚠ --llm requested but the synthesis fell back to the deterministic "
                f"(approx) intents — {_nerr}. Check the model key/name in THIS shell.[/yellow]")
        if json_out:
            import json as _json
            json_out.write_text(_json.dumps(payload, indent=2))
        if upload:
            try:
                res = upload_run(payload, api_url=api_url, api_key=key)
                if res.get("dashboard_url") and not live_state["shown_url"]:
                    console.print(f"  [dim]live at {res['dashboard_url']}[/dim]")
                    live_state["shown_url"] = True
            except GovernanceUploadError as exc:
                console.print(f"  [red]upload failed:[/red] {exc}")
        return True

    # Two-speed loop: Tier-1 screens every --screen-every seconds (local, 0 tokens); the
    # ProofAgent evaluation + upload fire only every --interval seconds. A one-shot does both.
    screen_secs = max(3, screen_every)
    interval_secs = max(1, interval)
    last_mtime: float = -1.0
    last_synth = -1.0e18
    scan_no = 0
    if not once:
        console.print(
            f"[dim]Tier-1 screen every {screen_secs}s · ProofAgent evaluation + upload every {interval}s[/dim]")
    try:
        if once:
            _scan(1, live=True, upload_now=True)
            raise typer.Exit(code=0)
        while True:
            p, _ws, _k = resolve_src()
            mtime = p.stat().st_mtime if (p and p.exists()) else 0.0
            now = time.monotonic()
            due = (now - last_synth) >= interval_secs
            if mtime != last_mtime or due:
                scan_no += 1
                _scan(scan_no, live=True, upload_now=due)
                last_mtime = mtime
                if due:
                    last_synth = now
            time.sleep(screen_secs)
    except KeyboardInterrupt:
        console.print("\n[dim]stopping — flushing a final snapshot (live=false)…[/dim]")
        with contextlib.suppress(Exception):
            _scan(scan_no + 1, live=False, upload_now=True)
    raise typer.Exit(code=0)


def _session_scorecard(result, tool: str, screens: list[str], upload: bool) -> None:
    """Terminal scorecard for a session: risk, dimension scores, findings, access map."""
    c = result.counts_by_severity
    t = Table(
        title=f"ProofAgent Harness - session ({tool})",
        title_style="bold cyan", title_justify="left",
        show_header=False, box=box.ROUNDED, padding=(0, 1),
    )
    t.add_column(style="dim", no_wrap=True)
    t.add_column(style="bold")
    t.add_row("Events screened", f"{len(result.events)}  (Tier-1 - 0 tokens)")
    t.add_row("Risk", f"{result.risk_score}/10   -   {result.certification}")
    t.add_row("Findings", f"{c['critical']} critical - {c['high']} high - {c['medium']} medium - {c['low']} low")
    for dim, sc in result.metric_scores.items():
        t.add_row(dim.replace("_", " ").title(), f"{sc}/10")
    am = result.access_map
    t.add_row("Access", f"{len(am['paths_written'])} written - {len(am['paths_read'])} read - "
                        f"{len(am['commands'])} cmds - {len(am['hosts'])} hosts")
    t2 = result.tier2 or {}
    if t2.get("status") == "scored":
        t.add_row("Tier-2", f"{t2.get('final_score')}/10  {t2.get('certification', '')}  "
                            f"({t2.get('finding_count', 0)} findings, "
                            f"{t2.get('token_usage', {}).get('total_tokens', 0)} tokens)")
    elif t2.get("triggered"):
        t.add_row("Tier-2", f"escalated on {t2.get('escalated_on')} - {t2.get('status')}"
                            + (f" ({t2.get('reason')})" if t2.get("reason") else ""))
    elif t2.get("status") == "disabled":
        t.add_row("Tier-2", "off (--assess never) - 0 tokens")
    else:
        t.add_row("Tier-2", "not triggered (no critical findings - 0 tokens)")
    console.print(t)
    for f in result.findings[:8]:
        col = {"critical": "red", "high": "red", "medium": "yellow", "low": "dim"}.get(f.severity, "white")
        console.print(f"  [{col}]● {f.severity.upper():<8}[/{col}] {f.title}")
    if len(result.findings) > 8:
        console.print(f"  [dim]… +{len(result.findings) - 8} more[/dim]")


def _session_markdown(result, tool: str) -> str:
    c = result.counts_by_severity
    lines = [
        f"# Session governance - {tool}",
        "",
        f"- Events screened: **{len(result.events)}** (Tier-1, 0 tokens)",
        f"- Risk: **{result.risk_score}/10** - {result.certification}",
        f"- Findings: {c['critical']} critical - {c['high']} high - {c['medium']} medium - {c['low']} low",
        "",
        "## Dimension scores",
        "",
        "| Dimension | Score |",
        "| --- | --- |",
    ]
    lines += [f"| {d.replace('_', ' ').title()} | {s}/10 |" for d, s in result.metric_scores.items()]
    lines += ["", f"## Findings ({len(result.findings)})", ""]
    for f in result.findings:
        lines.append(f"- **[{f.severity}]** {f.title} - `{f.finding_type}`")
    return "\n".join(lines) + "\n"


def _upload_session_and_gate(payload: dict, *, api_key: str | None, fail_on: str) -> None:
    """Upload a prebuilt session payload, print the gate decision, exit with its code."""
    from proofagent_harness.governance import (
        DEFAULT_API_BASE_URL,
        GovernanceUploadError,
        gate_exit_code,
        upload_run,
    )

    api_url = os.environ.get("PROOFAGENT_API_BASE_URL", "").rstrip("/") or DEFAULT_API_BASE_URL
    if not api_key:
        console.print(
            "[red]--upload was set but no API key was provided. Pass --api-key or set "
            f"PROOFAGENT_API_KEY (get one at {api_url} → Settings → API Keys).[/red]")
        raise typer.Exit(code=2)
    if fail_on not in ("pass", "review", "block"):
        console.print(f"[red]--fail-on must be pass | review | block (got {fail_on!r}).[/red]")
        raise typer.Exit(code=2)

    console.print(f"[dim]Uploading session to {api_url} …[/dim]")
    try:
        result = upload_run(payload, api_url=api_url, api_key=api_key)
    except GovernanceUploadError as exc:
        console.print(f"[red]Governance upload failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    gate_status = str(result.get("gate_status", "")).lower()
    code = gate_exit_code(gate_status, fail_on=fail_on)
    color = {"pass": "green", "review": "yellow", "block": "red"}.get(gate_status, "white")
    console.print(f"\n[bold]Governance gate:[/bold] [{color}]{gate_status.upper() or 'UNKNOWN'}[/{color}]")
    if result.get("final_score") is not None:
        grade = result.get("grade_label", "")
        console.print(f"  Final score : {result.get('final_score')}{f' ({grade})' if grade else ''}")
    for rule in result.get("failed_rules") or []:
        console.print(f"    [red]- {rule}[/red]")
    if result.get("dashboard_url"):
        console.print(f"  Dashboard   : {result.get('dashboard_url')}")
    console.print(f"[dim]Exit code {code} (fail-on={fail_on}).[/dim]")
    raise typer.Exit(code=code)


def _resolve_compliance_frameworks(
    *,
    enabled: bool,
    explicit: list[str] | None,
    api_key: str | None,
    api_base: str | None,
    profile: str | None,
    agent: str | None,
    governance_frameworks: list[str] | None = None,
) -> tuple[list[str] | None, str]:
    """Resolve the compliance scope for ``--assess-compliance``. Precedence:

    1. explicit ``--frameworks`` (fully local — always wins);
    2. the attached Agent Governance Profile's frameworks-in-scope (governance as
       code — the risk classification decides which regulations apply);
    3. the GOVERNANCE PLATFORM's profile selection, fetched from
       ``GET /compliance/selection`` when an API key is present (this is the
       "linked to my platform" path — brings back the framework ids + profile name);
    4. otherwise ``None`` → the harness uses its LOCAL default core set (pure
       open-source, no network call).

    Returns ``(framework_ids_or_None, human_source_label)``."""
    if explicit:
        return explicit, f"local · {len(explicit)} framework(s)"
    if governance_frameworks:
        return governance_frameworks, f"governance profile · {len(governance_frameworks)} framework(s)"
    if not enabled or not api_key:
        return None, "local core set"
    from proofagent_harness.governance import fetch_compliance_selection
    sel = fetch_compliance_selection(api_url=api_base, api_key=api_key, profile=profile, agent=agent)
    ids = [str(x) for x in (sel or {}).get("frameworks", []) if x]
    if not ids:
        return None, "local core set (platform returned no selection)"
    profiles = [p.get("name") for p in (sel or {}).get("profiles", []) if p.get("name")]
    pname = ((sel or {}).get("profile") or {}).get("name") or (profiles[0] if len(profiles) == 1 else None)
    label = (
        f"profile '{pname}'" if pname
        else f"{len(profiles)} platform policies" if len(profiles) > 1
        else "platform policy"
    )
    return ids, f"{label} · {len(ids)} framework(s)"


def _print_run_config(mode: str, rows: list[tuple[str, str]]) -> None:
    """Print a compact configuration table for context before the evaluation starts."""
    t = Table(
        title=f"ProofAgent Harness - {mode} evaluation",
        title_style="bold cyan", title_justify="left",
        show_header=False, box=box.ROUNDED, padding=(0, 1),
    )
    t.add_column(style="dim", no_wrap=True)
    t.add_column(style="bold")
    for k, v in rows:
        t.add_row(k, str(v))
    console.print(t)


def _transcript_text(report) -> str | None:
    """Best-effort flatten of a multi-turn transcript for evidence grounding."""
    turns = getattr(report, "transcript", None)
    if not turns:
        return None
    out = []
    for i, t in enumerate(turns):
        if isinstance(t, dict):
            idx = t.get("turn_index", i)
            u = t.get("question") or t.get("user") or t.get("probe") or t.get("prompt") or ""
            a = t.get("answer") or t.get("agent") or t.get("response") or t.get("output") or ""
        else:
            idx = getattr(t, "turn_index", i)
            u = getattr(t, "question", "") or getattr(t, "user", "") or ""
            a = getattr(t, "answer", "") or getattr(t, "agent", "") or getattr(t, "response", "") or ""
        out.append(f"[turn {idx}] USER: {u}\n[turn {idx}] AGENT: {a}")
    return ("\n".join(out)[:24000]) or None


def _print_context_engineering(report) -> None:
    """Echo the OPTIONAL context-engineering sub-score to the terminal when
    `--assess-context` produced one. It is also written to the Markdown report
    and the governance payload; this just surfaces it next to the scorecard."""
    ce = getattr(report, "context_engineering", None) or {}
    if not isinstance(ce, dict) or not ce.get("generated"):
        return
    arrows = {"big_cut": "↓↓", "cut": "↓", "neutral": "→", "adds": "↑"}
    savings = int(ce.get("token_savings_estimate") or 0)
    ctx_tokens = int(ce.get("context_tokens") or 0)
    pct = ce.get("token_savings_pct")
    head = f"[bold cyan]Context Engineering[/bold cyan]  {ce.get('score')}/10  ({ce.get('grade')})"
    if savings and pct is not None:
        head += f"   -   ~{savings:,} tokens reclaimable ({pct}% of the {ctx_tokens:,}-token context)"
    elif savings:
        head += f"   -   ~{savings:,} tokens reclaimable"
    console.print()
    console.print(head)
    if ce.get("summary"):
        console.print(f"  [dim]{ce['summary']}[/dim]")
    for s in ce.get("sub_criteria") or []:
        console.print(f"  {s.get('name', '')!s:<26} {s.get('score')}/10")
    for f in ce.get("findings") or []:
        a = arrows.get(str(f.get("token_impact", "neutral")), "→")
        console.print(f"  [{a}] [bold]{f.get('title', '')}[/bold]: {f.get('fix', '')}")
        if f.get("proof"):
            console.print(f"      [dim]proof: \"{f['proof']}\"[/dim]")
    console.print("  [dim](Separate sub-score - never affects the metric scores or the gate.)[/dim]")


def _upload_and_gate(
    report,
    *,
    api_key: str | None,
    agent_name: str,
    agent_version: str | None,
    profile: str | None,
    fail_on: str,
    source: str,
    environment: str | None = None,
    artifact_text: str | None = None,
    knowledge_text: str | None = None,
    transcript: str | None = None,
) -> None:
    """Build the governance payload, upload it, print the gate decision, and
    exit with the gate-mapped code. Always raises ``typer.Exit``."""
    from proofagent_harness.governance import (
        DEFAULT_API_BASE_URL,
        GovernanceUploadError,
        build_governance_payload,
        gate_exit_code,
        structure_findings_evidence,
        upload_run,
    )

    # Upload target: ProofAgent Cloud by default. `PROOFAGENT_API_BASE_URL` repoints
    # it - needed for on-prem / Enterprise backends and for local end-to-end testing
    # against a self-hosted governance stack (e.g. http://localhost:8000). Cloud stays
    # the default when the env var is unset, so a normal user only needs an API key.
    api_url = os.environ.get("PROOFAGENT_API_BASE_URL", "").rstrip("/") or DEFAULT_API_BASE_URL

    if not api_key:
        console.print(
            "[red]--upload was set but no API key was provided. Pass --api-key "
            "or set PROOFAGENT_API_KEY (get one at "
            f"{api_url} → Settings → API Keys).[/red]"
        )
        raise typer.Exit(code=2)

    if fail_on not in ("pass", "review", "block"):
        console.print(
            f"[red]--fail-on must be one of pass | review | block (got {fail_on!r}).[/red]"
        )
        raise typer.Exit(code=2)

    payload = build_governance_payload(
        report,
        agent_name=agent_name,
        agent_version=agent_version,
        profile=profile,
        source=source,
        environment=environment,
    )

    # Evidence-driven findings: structure each finding into claim → ref →
    # contradiction → fix. Best-effort + no-op-safe; disable with PROOFAGENT_EVIDENCE=0.
    if os.environ.get("PROOFAGENT_EVIDENCE", "1") != "0":
        with contextlib.suppress(Exception):
            structure_findings_evidence(
                payload,
                artifact_text=artifact_text,
                knowledge_text=knowledge_text,
                transcript=transcript,
                model=os.environ.get("PROOFAGENT_EVIDENCE_LLM", "gpt-4.1-mini"),
            )

    console.print(f"[dim]Uploading run to {api_url} …[/dim]")
    try:
        result = upload_run(payload, api_url=api_url, api_key=api_key)
    except GovernanceUploadError as exc:
        console.print(f"[red]Governance upload failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    gate_status = str(result.get("gate_status", "")).lower()
    code = gate_exit_code(gate_status, fail_on=fail_on)

    color = {"pass": "green", "review": "yellow", "block": "red"}.get(gate_status, "white")
    console.print(
        f"\n[bold]Governance gate:[/bold] [{color}]{gate_status.upper() or 'UNKNOWN'}[/{color}]"
    )
    if result.get("final_score") is not None:
        grade = result.get("grade_label", "")
        grade_suffix = f" ({grade})" if grade else ""
        console.print(f"  Final score : {result.get('final_score')}{grade_suffix}")
    failed_rules = result.get("failed_rules") or []
    if failed_rules:
        console.print("  Failed rules:")
        for rule in failed_rules:
            console.print(f"    [red]- {rule}[/red]")
    if result.get("dashboard_url"):
        console.print(f"  Dashboard   : {result.get('dashboard_url')}")
    console.print(f"[dim]Exit code {code} (fail-on={fail_on}).[/dim]")

    raise typer.Exit(code=code)


# ── Agent Governance Profile (governance as code) ────────────────────────────

def _resolve_governance_profile(
    *, file: Path | None, assess: bool, agent: str | None, api_key: str | None, api_base: str | None
):
    """Resolve the run's Agent Governance Profile. Precedence: an explicit local
    --governance-profile file wins; else --assess-governance fetches the profile
    bound to --agent from the cloud (by name). Returns a GovernanceProfile or None."""
    from proofagent_harness.governance_profile import from_cloud, load_profile

    if file is not None:
        try:
            return load_profile(file)
        except Exception as exc:
            console.print(f"[red]Could not load --governance-profile {file}:[/red] {exc}")
            raise typer.Exit(code=2) from exc
    if assess:
        if not api_key:
            console.print(
                "[red]--assess-governance needs an API key (--api-key / PROOFAGENT_API_KEY) "
                "to fetch the agent's bound profile from the cloud.[/red]"
            )
            raise typer.Exit(code=2)
        from proofagent_harness.governance import fetch_agent_governance
        data = fetch_agent_governance(
            api_url=api_base or None, api_key=api_key, agent=agent or "",
        )
        if not data or not data.get("classification"):
            console.print(
                f"[yellow]--assess-governance: no governance profile is bound to "
                f"agent '{agent}' in the cloud (nothing to gate against).[/yellow]"
            )
            return None
        return from_cloud(
            name=data.get("name") or (agent or "Agent Governance Profile"),
            classification=data["classification"],
            intake=data.get("intake") or {},
        )
    return None


def _print_governance_card(profile) -> None:
    """Render the Agent Governance Profile (risk classification) before the run —
    the tier + guardrails that will steer the traps and gate the release."""
    from rich.panel import Panel
    from rich.table import Table

    c = profile.controls
    color = {"low": "green", "medium": "yellow", "high": "dark_orange", "critical": "red"}.get(
        profile.risk_level, "white"
    )
    body = Table.grid(padding=(0, 2))
    body.add_column(justify="right", style="dim")
    body.add_column()
    body.add_row("Tier", f"[{color}]{profile.tier_label}[/{color}]  (risk level: {profile.risk_level})")
    body.add_row("Use case", str(profile.classification.get("use_case_label", profile.use_case)))
    body.add_row("Frameworks", ", ".join(profile.frameworks) or "—")
    body.add_row(
        "Guardrails",
        f"sign-off {'required' if c.get('signoff_required') else 'optional'} · "
        f"min score {c.get('min_final_score', '—')}/10 · "
        f"blocks on {c.get('block_severity', '—')}+ · "
        f"re-eval {c.get('continuous_assurance', '—')}",
    )
    if profile.prohibited:
        body.add_row("", "[bold red]Prohibited under EU AI Act Article 5 — do not deploy.[/bold red]")
    console.print(
        Panel(body, title=f"[bold]Agent Governance Profile[/bold] · {profile.name}",
              subtitle="risk classification · steers traps + context bar, gates the release",
              border_style=color)
    )


def _print_governance_verdict(profile, gate) -> None:
    color = {"pass": "green", "review": "yellow", "block": "red"}.get(gate.decision, "white")
    console.print(
        f"\n[bold]Agent Governance gate:[/bold] [{color}]{gate.decision.upper()}[/{color}] "
        f"[dim](tier: {profile.tier_label})[/dim]"
    )
    for r in gate.reasons:
        console.print(f"  [{color}]•[/{color}] {r}")
    if gate.failed_rules:
        console.print("  Failed rules: " + ", ".join(f"[red]{r}[/red]" for r in gate.failed_rules))


def _upload_run_best_effort(
    report, *, api_key: str | None, agent_name: str | None, agent_version: str | None,
    source: str, environment: str | None, transcript: str | None, profile,
    profile_slug: str | None = None,
) -> None:
    """Upload the run WITH the attached Agent Governance Profile embedded, without
    gating/exiting (the local governance gate is authoritative). Best-effort."""
    if not api_key:
        console.print("[yellow]--upload skipped: no API key.[/yellow]")
        return
    from proofagent_harness.governance import (
        DEFAULT_API_BASE_URL,
        build_governance_payload,
        structure_findings_evidence,
        upload_run,
    )
    from proofagent_harness.governance_profile import to_payload

    api_url = os.environ.get("PROOFAGENT_API_BASE_URL", "").rstrip("/") or DEFAULT_API_BASE_URL
    payload = build_governance_payload(
        report, agent_name=agent_name, agent_version=agent_version,
        profile=profile_slug, source=source, environment=environment,
    )
    payload["agent_governance_profile"] = to_payload(profile)  # rides up for the dashboard card

    # Evidence-driven findings — same treatment as the plain upload path, so a
    # governance-profile run is never a poorer record on the dashboard.
    # Best-effort + no-op-safe; disable with PROOFAGENT_EVIDENCE=0.
    if os.environ.get("PROOFAGENT_EVIDENCE", "1") != "0":
        with contextlib.suppress(Exception):
            structure_findings_evidence(
                payload,
                transcript=transcript,
                model=os.environ.get("PROOFAGENT_EVIDENCE_LLM", "gpt-4.1-mini"),
            )

    console.print(f"[dim]Uploading run (+ governance profile) to {api_url} …[/dim]")
    try:
        result = upload_run(payload, api_url=api_url, api_key=api_key)
        if result.get("dashboard_url"):
            console.print(f"  Dashboard   : {result.get('dashboard_url')}")
    except Exception as exc:  # best-effort — never fail the run on upload trouble
        console.print(f"[yellow]Upload failed (run gated locally anyway):[/yellow] {exc}")


def _governance_gate_and_exit(
    report, profile, *, fail_on: str, upload: bool, api_key: str | None,
    agent_name: str | None, agent_version: str | None, source: str,
    environment: str | None, transcript: str | None = None,
    profile_slug: str | None = None,
) -> None:
    """Apply the LOCAL Agent Governance Profile gate (authoritative), optionally
    uploading first. Always raises typer.Exit."""
    from proofagent_harness.governance import gate_exit_code

    if upload:
        _upload_run_best_effort(
            report, api_key=api_key, agent_name=agent_name, agent_version=agent_version,
            source=source, environment=environment, transcript=transcript, profile=profile,
            profile_slug=profile_slug,
        )
    gate = profile.gate(
        final_score=getattr(report, "final_score", None),
        findings=getattr(report, "findings", None),
    )
    _print_governance_verdict(profile, gate)
    code = gate_exit_code(gate.decision, fail_on=fail_on)
    console.print(f"[dim]Exit code {code} (fail-on={fail_on}).[/dim]")
    raise typer.Exit(code=code)

@traps_app.command("list")
def traps_list(
    family: str | None = typer.Option(None, "--family"),
) -> None:
    """List bundled traps."""
    traps = load_traps()
    if family:
        traps = [t for t in traps if t.family == family]

    table = Table(title="Bundled traps", show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Family")
    table.add_column("Severity")
    table.add_column("Metrics")
    for t in traps:
        table.add_row(t.name, t.family, t.severity, ", ".join(t.metrics))
    console.print(table)

@traps_app.command("show")
def traps_show(name: str = typer.Argument(...)) -> None:
    """Show details for a specific trap."""
    traps = load_traps()
    for t in traps:
        if t.name == name:
            console.print(t.model_dump())
            return
    console.print(f"[red]Trap not found: {name}[/red]")
    raise typer.Exit(code=1)

@traps_app.command("install")
def traps_install(pack: str = typer.Argument(...)) -> None:
    """Install a community trap pack via pip."""
    import subprocess

    pkg = f"proofagent-traps-{pack}"
    console.print(f"[dim]Running: pip install {pkg}[/dim]")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", pkg])
    raise typer.Exit(code=rc)

@traps_app.command("domains")
def traps_domains() -> None:
    """Show the domain-to-traps mapping for the bundled library."""
    idx = load_trap_index()

    table = Table(title="Traps by domain", show_header=True, header_style="bold")
    table.add_column("Domain")
    table.add_column("Trap count", justify="right")
    table.add_column("Traps")

    for domain in sorted(idx.by_domain.keys()):
        names = sorted(t.name for t in idx.by_domain[domain])
        preview = ", ".join(names[:5])
        if len(names) > 5:
            preview += f", ... (+{len(names) - 5})"
        table.add_row(domain, str(len(names)), preview)

    console.print(table)

    universal_table = Table(
        title="Universal traps (always selected)",
        show_header=True,
        header_style="bold",
    )
    universal_table.add_column("Trap")
    universal_table.add_column("Family")
    for t in sorted(idx.universals, key=lambda x: (x.family, x.name)):
        universal_table.add_row(t.name, t.family)
    console.print(universal_table)

@traps_app.command("stats")
def traps_stats() -> None:
    """Print summary stats for the trap library."""
    idx = load_trap_index()
    s = idx.stats()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for k, v in s.items():
        table.add_row(k.replace("_", " ").title(), str(v))
    console.print(table)

@traps_app.command("validate")
def traps_validate(
    path: Path | None = typer.Argument(
        None,
        help="A directory of trap .md files OR a single trap .md file. "
             "Defaults to the bundled library.",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Treat warnings as errors (exit non-zero on any warning)."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Only print files with errors or warnings."
    ),
) -> None:
    """Validate trap manifests against the canonical schema.

    See ``proofagent_harness.trap_schema`` for the contract. Exits with a
    non-zero status when any file has errors (or, with ``--strict``, any
    warnings) - suitable for CI.
    """
    from proofagent_harness.trap_schema import (
        TrapLibraryValidation,
        validate_trap_file,
        validate_trap_library,
    )

    if path is None:
        from importlib import resources

        with resources.as_file(
            resources.files("proofagent_harness").joinpath("data/traps")
        ) as p:
            root = Path(p)
        result = validate_trap_library(root)
    elif path.is_file():
        # B4 (client report): a single trap .md file is valid input, not only
        # a directory - previously this printed "No trap .md files found".
        root = path
        result = TrapLibraryValidation(results=[validate_trap_file(path)])
    elif path.is_dir():
        root = path
        result = validate_trap_library(root)
    else:
        console.print(f"[red]Path not found: {path}[/red]")
        raise typer.Exit(code=1)

    if not result.results:
        msg = (
            f"No trap .md files found under {root}"
            if root.is_dir()
            else f"Not a .md trap file: {root}"
        )
        console.print(f"[yellow]{msg}[/yellow]")
        raise typer.Exit(code=1)

    table = Table(show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Status", justify="center")
    table.add_column("Findings")

    for r in result.results:
        if quiet and r.ok and not r.warnings:
            continue
        status = (
            "[red]FAIL[/red]"
            if r.errors
            else ("[yellow]warn[/yellow]" if r.warnings else "[green]ok[/green]")
        )
        rows: list[str] = []
        for e in r.errors:
            rows.append(f"[red]✗[/red] {e}")
        for w in r.warnings:
            rows.append(f"[yellow]⚠[/yellow] {w}")
        table.add_row(
            str(r.path.relative_to(root)),
            status,
            "\n".join(rows) if rows else "-",
        )
    console.print(table)
    console.print(
        f"\n[bold]{len(result.results)}[/bold] traps - "
        f"[red]{result.error_count}[/red] errors - "
        f"[yellow]{result.warning_count}[/yellow] warnings"
    )

    if result.error_count or (strict and result.warning_count):
        raise typer.Exit(code=1)

@app.command("metrics")
def metrics_list() -> None:
    """List the canonical metrics this harness scores."""
    table = Table(title="Canonical metrics", show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Description")
    for m in CANONICAL_METRICS:
        table.add_row(m, METRIC_DESCRIPTIONS.get(m, ""))
    console.print(table)

@app.command("version")
def version() -> None:
    """Print the package version."""
    console.print(f"proofagent-harness {__version__}")

def _load_callable(path: Path, name: str):
    spec = importlib.util.spec_from_file_location("user_agent", path)
    if spec is None or spec.loader is None:
        raise typer.BadParameter(f"Could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, name):
        raise typer.BadParameter(
            f"{path} has no attribute named {name!r}. "
            "Define `agent` (or pass --entry) as a callable."
        )
    obj = getattr(mod, name)
    if not callable(obj):
        raise typer.BadParameter(f"{name!r} in {path} is not callable.")
    return obj

if __name__ == "__main__":
    app()
