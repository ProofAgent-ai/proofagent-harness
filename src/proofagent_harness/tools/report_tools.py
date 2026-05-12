"""Render Reports to terminal (Rich), Markdown, and HTML."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from proofagent_harness.schemas import Certification, Report, Severity

# ─────────────────────────────────────────────────────────────────────────────
# Rich (terminal)
# ─────────────────────────────────────────────────────────────────────────────


_SEV_STYLES = {
    Severity.CRITICAL: "bold red",
    Severity.FAIL: "red",
    Severity.WARN: "yellow",
    Severity.PASS: "green",
}

_CERT_STYLES = {
    Certification.GOLD: "bold gold1",
    Certification.SILVER: "bold green",
    Certification.NEEDS_ENHANCEMENT: "yellow",
    Certification.NOT_READY: "bold red",
}


def render_rich(report: Report) -> Any:
    """Build a Rich renderable for terminal printing.

    Metrics where every juror failed (consensus_log[m].evaluated == False)
    are surfaced as "N/A" rather than as a fake score. This keeps the
    scorecard honest when something went wrong mid-run.
    """
    from proofagent_harness.schemas import CANONICAL_METRICS

    table = Table(
        title="ProofAgent Harness — Scorecard",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("Severity", justify="left")

    # Show every canonical metric (or every metric the run targeted) in the
    # table — including ones flagged not-evaluated. Customers want a complete
    # row map, with "N/A" where evaluation failed.
    metrics_in_run = (
        list(report.per_metric.keys())
        + [
            m
            for m in report.consensus_log
            if m not in report.per_metric
        ]
    )
    if not metrics_in_run:
        metrics_in_run = list(CANONICAL_METRICS)

    for metric in metrics_in_run:
        cl = report.consensus_log.get(metric)
        evaluated = cl.evaluated if cl else (metric in report.per_metric)

        if not evaluated:
            table.add_row(
                metric.replace("_", " ").title(),
                Text("N/A", style="dim"),
                Text("—", style="dim"),
                Text("not evaluated", style="dim italic"),
            )
            continue

        score = report.per_metric.get(metric, 0.0)
        sev = report.severity.get(metric, Severity.PASS)
        conf = report.confidence.get(metric, 0.0)
        table.add_row(
            metric.replace("_", " ").title(),
            f"{score:.1f} / 10",
            f"{conf:.2f}",
            Text(sev.value, style=_SEV_STYLES.get(sev, "")),
        )

    cert_text = Text(
        report.certification.value,
        style=_CERT_STYLES.get(report.certification, "white"),
    )

    cert_line = Text()
    cert_line.append(f"Final score: {report.final_score:.2f} / 10    ", style="bold")
    cert_line.append("Certification: ", style="bold")
    cert_line.append(cert_text)
    cert_line.append(f"    Tokens: {report.tokens_used:,}", style="dim")

    blocks: list[Any] = [table, Text(""), cert_line]

    # Surface report-level warnings prominently — plateau detection,
    # juror dissent, same-model bias hints, etc.
    if report.warnings:
        blocks.append(Text(""))
        blocks.append(Text("Warnings:", style="bold yellow"))
        for w in report.warnings:
            blocks.append(Text(f"  - {w}", style="yellow"))

    return Panel(
        Group(*blocks),
        title="proofagent-harness",
        border_style="cyan",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Markdown
# ─────────────────────────────────────────────────────────────────────────────


def render_markdown(report: Report) -> str:
    """Render a Markdown report — good for PR comments and docs."""
    lines: list[str] = []
    lines.append("# ProofAgent Harness — Evaluation Report\n")

    lines.append(f"**Final score:** `{report.final_score:.2f} / 10`  ")
    lines.append(f"**Certification:** `{report.certification.value}`  ")
    lines.append(f"**Tokens used:** `{report.tokens_used}`  ")
    lines.append(f"**Duration:** `{report.duration_seconds:.1f}s`\n")

    lines.append(f"> {report.summary}\n")

    if report.warnings:
        lines.append("## Warnings\n")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## Per-metric scores\n")
    lines.append("| Metric | Score | Confidence | Severity |")
    lines.append("|---|---|---|---|")

    # Show every metric in the run — including N/A rows for any whose juror
    # calls all failed (so the report stays honest about coverage).
    metrics_in_run = (
        list(report.per_metric.keys())
        + [m for m in report.consensus_log if m not in report.per_metric]
    )
    for metric in metrics_in_run:
        cl = report.consensus_log.get(metric)
        evaluated = cl.evaluated if cl else (metric in report.per_metric)
        pretty = metric.replace("_", " ").title()
        if not evaluated:
            lines.append(f"| {pretty} | N/A | — | not evaluated |")
            continue
        score = report.per_metric.get(metric, 0.0)
        sev = report.severity.get(metric, Severity.PASS).value
        conf = report.confidence.get(metric, 0.0)
        lines.append(
            f"| {pretty} | {score:.1f} / 10 | {conf:.2f} | {sev} |"
        )
    lines.append("")

    if report.findings:
        lines.append("## Findings\n")
        for f in report.findings:
            lines.append(f"### {f.headline}")
            lines.append(f"- **Detail:** {f.detail}")
            if f.recommendation:
                lines.append(f"- **Recommendation:** {f.recommendation}")
            lines.append("")

    if report.transcript:
        lines.append("## Transcript\n")
        for t in report.transcript:
            lines.append(f"### Turn {t.turn_index} · trap: `{t.trap_name}`")
            lines.append(f"**Q:** {t.question}\n")
            lines.append(f"**A:** {t.answer}\n")
            if t.defects:
                lines.append(f"_Defects: {', '.join(t.defects)}_\n")

    return "\n".join(lines)
