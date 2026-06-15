"""Typer CLI — the `proof` command."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from proofagent_harness import (
    CANONICAL_METRICS,
    METRIC_DESCRIPTIONS,
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
    turns: int = typer.Option(8, "--turns", min=1, max=50),
    consensus: str = typer.Option("delphi", "--consensus", help="independent | delphi | debate"),
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
    knowledge: Path | None = typer.Option(None, "--knowledge", exists=True),
    llm: str | None = typer.Option(None, "--llm", help="Model id (LiteLLM target)."),
    json_out: Path | None = typer.Option(None, "--json", help="Write report JSON to this path."),
    md_out: Path | None = typer.Option(None, "--markdown", help="Write report Markdown to this path."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress live progress UI."),
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
        metrics=metric_list,
        turns=turns,
        consensus=consensus,
        extra_traps=_csv(extra_traps),
        trap_packs=_csv(trap_packs),
        pin_traps=_csv(pin_traps),
        verbose=not quiet,
    )

    report = harness.evaluate(
        callable_obj,
        role=role,
        business_case=business_case,
        goal=goal,
        knowledge=str(knowledge) if knowledge else None,
    )

    if json_out:
        report.to_json(str(json_out))
        console.print(f"[dim]Report JSON written to {json_out}[/dim]")
    if md_out:
        report.to_markdown(str(md_out))
        console.print(f"[dim]Report Markdown written to {md_out}[/dim]")

    raise typer.Exit(code=0 if report.certification.value != "NOT_READY" else 1)

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
