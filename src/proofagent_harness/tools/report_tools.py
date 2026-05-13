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

    # Severity summary — single line giving the operator the at-a-glance signal
    # (X turns soft-failed, Y phantom-tool, Z findings) without scrolling.
    sev_line = _build_severity_summary_line(report)
    if sev_line is not None:
        blocks.append(sev_line)

    # Compact warnings — show 1 line per warning (first sentence only) in the
    # live terminal. Full text + code snippets live in the saved markdown report
    # (render_markdown). Keeps the live scorecard readable.
    if report.warnings:
        blocks.append(Text(""))
        blocks.append(
            Text(
                f"Warnings ({len(report.warnings)}) — see markdown report for full detail + fix snippets:",
                style="bold yellow",
            )
        )
        for w in report.warnings:
            first_line = _first_sentence(w)
            blocks.append(Text(f"  • {first_line}", style="yellow"))

    # One-line "what to fix next" hint
    next_step = _next_step_hint(report)
    if next_step is not None:
        blocks.append(Text(""))
        blocks.append(Text(f"Next: {next_step}", style="bold cyan"))

    return Panel(
        Group(*blocks),
        title="proofagent-harness",
        border_style="cyan",
    )


def _first_sentence(text: str, max_chars: int = 140) -> str:
    """Pick the first sentence (or first line) of a long warning, capped."""
    text = text.strip().replace("\n", " ")
    # Sentence break — take up to first period that's followed by space + capital
    for i, ch in enumerate(text):
        if ch in ".!?" and i + 1 < len(text) and text[i + 1] == " ":
            return text[: i + 1] if i + 1 <= max_chars else text[: max_chars - 1] + "…"
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _build_severity_summary_line(report: Report) -> Text | None:
    """Single line counting per-turn defects across the transcript.

    Surfaces what the operator most needs to see at a glance: how many
    turns had observable failures of each kind. Without this, defect
    flags are buried in the per-turn JSON and easy to miss.
    """
    transcript = report.transcript or []
    if not transcript:
        return None

    defect_counts: dict[str, int] = {}
    for t in transcript:
        for d in (t.defects or []):
            # Trim long defect strings (e.g., "expected_tool_missing:foo,bar")
            key = d.split(":")[0]
            defect_counts[key] = defect_counts.get(key, 0) + 1

    if not defect_counts:
        return None

    # Compose: "Defects: 3 phantom_tool_call_claimed, 1 possible_system_prompt_echo (across 15 turns)"
    parts = sorted(defect_counts.items(), key=lambda kv: -kv[1])
    body = ", ".join(f"{count} {name}" for name, count in parts)
    line = Text()
    line.append("Defects: ", style="bold red")
    line.append(body, style="red")
    line.append(f" (across {len(transcript)} turns)", style="dim")
    return line


def _next_step_hint(report: Report) -> str | None:
    """Pick the single most useful next-step suggestion for the operator.

    Priority order (most actionable first):
      1. Critical / fail metric → fix that metric
      2. Limited-context warning → add the missing AgentContext field
      3. Plateau warning → run weak-agent baseline for calibration check
      4. Phantom tool calls → check tool-use wiring
    """
    sev_order = ["critical", "fail", "warn"]
    severity_dict = {k: v.value if hasattr(v, "value") else str(v)
                     for k, v in (report.severity or {}).items()}
    for sev in sev_order:
        for metric, s in severity_dict.items():
            if s == sev:
                return f"address `{metric}` ({sev}) — see findings for the offending turn(s)"

    # No severity issues — check for context gaps
    warnings_text = " ".join(report.warnings or []).lower()
    if "no system_prompt" in warnings_text:
        return "pass `context=AgentContext(system_prompt=...)` for full instruction_following scoring"
    if "no knowledge corpus" in warnings_text:
        return "pass `knowledge='./your_policy_dir/'` to enable corpus-grounded factuality"
    if "no tool schemas" in warnings_text:
        return "pass `context=AgentContext(tools=[...])` to enable tool-boundary scoring"
    if "plateau" in warnings_text:
        return "run examples/06_weak_agent_baseline.py to verify discrimination gap >=3"

    transcript = report.transcript or []
    phantom = sum(1 for t in transcript if "phantom_tool_call_claimed" in (t.defects or []))
    if phantom > 0:
        return f"check tool-use wiring — {phantom} turn(s) had phantom tool claims"

    return None


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
