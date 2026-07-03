"""Typer CLI — the `proof` command."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from pathlib import Path

import typer
from rich import box
from rich.console import Console
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
    help="proofagent-harness — open-source test harness for AI agents.",
    no_args_is_help=True,
    add_completion=False,
)

traps_app = typer.Typer(name="traps", help="Manage trap libraries.", no_args_is_help=True)
app.add_typer(traps_app)

console = Console()

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
             "selection scoring (client report B2 — pin a custom trap that would "
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
        help="Directory of DOMAIN KNOWLEDGE the agent is grounded on (policies, specs, FAQs — "
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
    # ── Governance upload (gate CI/CD on the release decision) ──
    # OFF by default — a vanilla `proof run` stays fully local, no network.
    upload: bool = typer.Option(
        False, "--upload/--no-upload",
        help="Upload the result to the ProofAgent Governance API and gate on "
             "the returned decision (exit 0 pass / 1 review / 2 block).",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key",
        help="API key for the Governance API. Defaults to env PROOFAGENT_API_KEY. "
             "Get a key at https://app.proofagent.ai -> Settings -> API Keys.",
    ),
    agent: str | None = typer.Option(
        None, "--agent",
        help="Name of the agent under test — this is the name shown on the governance "
             "dashboard, and it groups runs + regressions together. Defaults to --role.",
    ),
    agent_version: str | None = typer.Option(
        None, "--agent-version", help="Version / git ref of the agent under test."),
    profile: str | None = typer.Option(
        None, "--profile",
        help="Governance profile slug to evaluate against (e.g. "
             "airline_customer_support)."),
    fail_on: str = typer.Option(
        "block", "--fail-on",
        help="Which gate decision fails the build: pass | review | block. "
             "Default 'block' (review is informational)."),
    source: str = typer.Option(
        "ci_cd", "--source",
        help="Origin of this run: local | ci_cd | manual | api | scheduled."),
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

    if not quiet:
        cfg = [
            ("Agent file", f"{agent_file}  (entry: {entry})"),
            ("Harness LLM", llm or "(default)"),
            ("Fallback LLM", fallback_llm or os.environ.get("PROOFAGENT_FALLBACK_LLM") or "—"),
            ("Turns", str(turns)),
            ("Consensus", consensus),
            ("Seed", str(seed) if seed is not None else "—"),
            ("Metrics", ", ".join(metric_list)),
            ("Context dir", str(context_dir) if context_dir else "—"),
            ("Domain knowledge", str(domain_knowledge_dir) if domain_knowledge_dir else "—"),
            ("Assess context", "yes" if assess_context else "no"),
            ("Role", eff_role),
            ("Upload", "yes  →  app.proofagent.ai" if upload else "no (local only)"),
        ]
        if upload:
            cfg += [
                ("Agent (dashboard)", agent or eff_role),
                ("Agent version", agent_version or "—"),
                ("Gate profile", profile or "—"),
                ("Fail on", fail_on),
            ]
        _print_run_config("multi-turn", cfg)

    report = harness.evaluate(
        callable_obj,
        role=eff_role,
        business_case=eff_business,
        goal=eff_goal,
        context=ctx,          # the agent: system_prompt + tools + memory
        knowledge=str(domain_knowledge_dir) if domain_knowledge_dir else None,
        assess_context=assess_context,
    )

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
             "(policies, specs, source data — .md / .txt / .json / .yaml). "
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

    if not quiet:
        cfg = [
            ("Artifact", f"{artifact_path}  (type: {artifact_type})"),
            ("Harness LLM", llm or "(default)"),
            ("Fallback LLM", fallback_llm or os.environ.get("PROOFAGENT_FALLBACK_LLM") or "—"),
            ("Consensus", consensus),
            ("Seed", str(seed)),
            ("Domain knowledge", str(knowledge_dir) if knowledge_dir else "—"),
            ("Assess context", "yes" if assess_context else "no"),
            ("Role", role),
            ("Upload", "yes  →  app.proofagent.ai" if upload else "no (local only)"),
        ]
        if upload:
            cfg += [
                ("Agent (dashboard)", agent or role),
                ("Agent version", agent_version or "—"),
                ("Gate profile", profile or "—"),
                ("Fail on", fail_on),
            ]
        _print_run_config("artifact", cfg)

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
    )

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


def _print_run_config(mode: str, rows: list[tuple[str, str]]) -> None:
    """Print a compact configuration table for context before the evaluation starts."""
    t = Table(
        title=f"ProofAgent Harness — {mode} evaluation",
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
    head = f"[bold cyan]Context Engineering[/bold cyan]  {ce.get('score')}/10  ({ce.get('grade')})"
    if savings:
        head += f"   ·   ~{savings:,} tokens reclaimable"
    console.print()
    console.print(head)
    if ce.get("summary"):
        console.print(f"  [dim]{ce['summary']}[/dim]")
    for s in ce.get("sub_criteria") or []:
        console.print(f"  {s.get('name', '')!s:<26} {s.get('score')}/10")
    for f in ce.get("findings") or []:
        a = arrows.get(str(f.get("token_impact", "neutral")), "→")
        console.print(f"  [{a}] [bold]{f.get('title', '')}[/bold]: {f.get('fix', '')}")
    console.print("  [dim](Separate sub-score — never affects the metric scores or the gate.)[/dim]")


def _upload_and_gate(
    report,
    *,
    api_key: str | None,
    agent_name: str,
    agent_version: str | None,
    profile: str | None,
    fail_on: str,
    source: str,
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

    # The upload target is hard-locked to ProofAgent Cloud. `--upload` always
    # pushes here — there is deliberately NO flag or env var to repoint it, so a
    # user only needs an API key. (On-prem / Enterprise deployments target their
    # own backend by calling upload_run(api_url=…) from their bundle, not through
    # this public CLI path.)
    api_url = DEFAULT_API_BASE_URL

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
    warnings) — suitable for CI.
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
        # a directory — previously this printed "No trap .md files found".
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
            "\n".join(rows) if rows else "—",
        )
    console.print(table)
    console.print(
        f"\n[bold]{len(result.results)}[/bold] traps · "
        f"[red]{result.error_count}[/red] errors · "
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
