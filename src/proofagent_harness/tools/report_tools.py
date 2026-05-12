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
    """Build a Rich renderable for terminal printing."""
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

    for metric, score in report.per_metric.items():
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

    return Panel(
        Group(table, Text(""), cert_line),
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
    lines.append(f"**Cost:** `${report.cost_usd:.4f}`  ")
    lines.append(f"**Tokens used:** `{report.tokens_used}`  ")
    lines.append(f"**Duration:** `{report.duration_seconds:.1f}s`\n")

    lines.append(f"> {report.summary}\n")

    lines.append("## Per-metric scores\n")
    lines.append("| Metric | Score | Confidence | Severity |")
    lines.append("|---|---|---|---|")
    for metric, score in report.per_metric.items():
        sev = report.severity.get(metric, Severity.PASS).value
        conf = report.confidence.get(metric, 0.0)
        lines.append(
            f"| {metric.replace('_', ' ').title()} | {score:.1f} / 10 | {conf:.2f} | {sev} |"
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
