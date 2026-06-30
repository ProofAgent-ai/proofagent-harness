"""Render Reports to terminal (Rich), Markdown, and HTML."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from proofagent_harness.schemas import (
    ARTIFACT_METRIC_DESCRIPTIONS,
    METRIC_DESCRIPTIONS,
    Certification,
    Report,
    Severity,
)

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
    Certification.INCOMPLETE: "bold white on grey37",
}


def _score_display(report: Report) -> str:
    """The score string — '—' for an INCOMPLETE run (nothing was scored), so a
    placeholder 0.0 is never shown as if it were an agent grade."""
    if report.certification == Certification.INCOMPLETE:
        return "— (not scored)"
    return f"{report.final_score:.2f} / 10"

def render_rich(report: Report) -> Any:
    """Build a Rich renderable for terminal printing."""
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
    cert_line.append(f"Final score: {_score_display(report)}    ", style="bold")
    cert_line.append("Certification: ", style="bold")
    cert_line.append(cert_text)
    cert_line.append(f"    Tokens: {report.tokens_used:,}", style="dim")

    blocks: list[Any] = [table, Text(""), cert_line]

    sev_line = _build_severity_summary_line(report)
    if sev_line is not None:
        blocks.append(sev_line)

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
    for i, ch in enumerate(text):
        if ch in ".!?" and i + 1 < len(text) and text[i + 1] == " ":
            return text[: i + 1] if i + 1 <= max_chars else text[: max_chars - 1] + "…"
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

def _build_severity_summary_line(report: Report) -> Text | None:
    """Single line counting per-turn defects across the transcript."""
    transcript = report.transcript or []
    if not transcript:
        return None

    defect_counts: dict[str, int] = {}
    for t in transcript:
        for d in (t.defects or []):
            key = d.split(":")[0]
            defect_counts[key] = defect_counts.get(key, 0) + 1

    if not defect_counts:
        return None

    parts = sorted(defect_counts.items(), key=lambda kv: -kv[1])
    body = ", ".join(f"{count} {name}" for name, count in parts)
    line = Text()
    line.append("Defects: ", style="bold red")
    line.append(body, style="red")
    line.append(f" (across {len(transcript)} turns)", style="dim")
    return line

def _next_step_hint(report: Report) -> str | None:
    """Pick the single most useful next-step suggestion for the operator."""
    # Highest priority: the harness LLM itself was refused by its provider
    # (content filter). No agent fix helps — the operator must switch the
    # harness LLM. Surfaced first so it isn't masked by a metric hint.
    cert = (report.certification.value if hasattr(report.certification, "value")
            else str(report.certification))
    wtext = " ".join(report.warnings or []).lower()
    if cert == "INCOMPLETE" or any(
        k in wtext for k in ("refused", "content filter", "cybersecurity", "content/safety")
    ):
        return ("switch the harness LLM to a cross-family Anthropic model "
                "(`--llm claude-sonnet-4-5`) or set `--fallback-llm claude-sonnet-4-5` "
                "— the current harness LLM was refused by its provider on this content")

    sev_order = ["critical", "fail", "warn"]
    severity_dict = {k: v.value if hasattr(v, "value") else str(v)
                     for k, v in (report.severity or {}).items()}
    for sev in sev_order:
        for metric, s in severity_dict.items():
            if s == sev:
                return f"address `{metric}` ({sev}) — see findings for the offending turn(s)"

    warnings_text = " ".join(report.warnings or []).lower()
    if "no system_prompt" in warnings_text:
        return "pass `context=AgentContext(system_prompt=...)` for full instruction_following scoring"
    if "no knowledge corpus" in warnings_text:
        return "pass `knowledge='./your_policy_dir/'` to enable corpus-grounded factuality"
    if "no tool schemas" in warnings_text:
        return "pass `context=AgentContext(tools=[...])` to enable tool-boundary scoring"
    if "plateau" in warnings_text:
        return "evaluate a deliberately weak agent and confirm its scores drop >=3 points — if not, the jurors may be over-scoring"

    transcript = report.transcript or []
    phantom = sum(1 for t in transcript if "phantom_tool_call_claimed" in (t.defects or []))
    if phantom > 0:
        return f"check tool-use wiring — {phantom} turn(s) had phantom tool claims"

    return None

def render_markdown(report: Report) -> str:
    """Render a Markdown report — good for PR comments and docs."""
    lines: list[str] = []
    lines.append("# ProofAgent Harness — Evaluation Report\n")

    # Header: the four numbers a reviewer wants at a glance, plus the
    # eval mode (artifact vs multi_turn) so the reader knows what the
    # numbers mean.
    lines.append(f"**Final score:** `{_score_display(report)}`  ")
    lines.append(f"**Certification:** `{report.certification.value}`  ")
    lines.append(f"**Mode:** `{report.mode}`  ")
    lines.append(f"**Tokens used:** `{report.tokens_used}`  ")
    lines.append(f"**Duration:** `{report.duration_seconds:.1f}s`\n")

    lines.append(f"> {report.summary}\n")

    if report.warnings:
        lines.append("## Warnings\n")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    # ── Compliance mapping (reporter-generated) ──────────────────────
    _comp = getattr(report, "compliance", None) or {}
    _fws = _comp.get("frameworks") if isinstance(_comp, dict) else None
    if _fws:
        lines.append("## Compliance\n")
        for fw in _fws:
            lines.append(f"### {fw.get('name', '')} — {fw.get('score', '?')}% coverage")
            if fw.get("summary"):
                lines.append(f"_{fw['summary']}_\n")
            lines.append("| Control | Status | Rationale |")
            lines.append("|---|---|---|")
            for c in fw.get("controls", []):
                ref = f"{c.get('ref', '')} {c.get('title', '')}".strip()
                lines.append(f"| {ref} | {c.get('status', '')} | {c.get('rationale', '')} |")
            lines.append("")

    # ── Context engineering (optional, reporter-generated) ───────────
    _ce = getattr(report, "context_engineering", None) or {}
    if isinstance(_ce, dict) and _ce.get("generated"):
        _impact = {"big_cut": "↓↓", "cut": "↓", "neutral": "→", "adds": "↑"}
        _savings = int(_ce.get("token_savings_estimate") or 0)
        head = f"## Context engineering — {_ce.get('score', '?')}/10 ({_ce.get('grade', '')})"
        if _savings:
            head += f" · ~{_savings:,} tokens reclaimable"
        lines.append(head)
        if _ce.get("summary"):
            lines.append(f"_{_ce['summary']}_\n")
        _subs = _ce.get("sub_criteria") or []
        if _subs:
            lines.append("| Criterion | Score |")
            lines.append("|---|---|")
            for s in _subs:
                lines.append(f"| {s.get('name', s.get('id', ''))} | {s.get('score', '?')}/10 |")
            lines.append("")
        _cf = _ce.get("findings") or []
        if _cf:
            lines.append("| Finding | Fix | Tokens |")
            lines.append("|---|---|---|")
            for f in _cf:
                lines.append(
                    f"| {f.get('title', '')} — {f.get('problem', '')} "
                    f"| {f.get('fix', '')} | {_impact.get(f.get('token_impact', 'neutral'), '→')} |"
                )
            lines.append("")

    # ── Per-metric scores ────────────────────────────────────────────
    lines.append("## Per-metric scores\n")
    lines.append("| Metric | Score | Confidence | Severity |")
    lines.append("|---|---|---|---|")

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

    # ── Metric definitions — mode-aware so readers know what each
    # score MEANS in their evaluation mode. v0.5.0 surfaces this so
    # business stakeholders reading an artifact report aren't left
    # decoding cryptic metric names.
    description_table = (
        ARTIFACT_METRIC_DESCRIPTIONS
        if report.mode == "artifact"
        else METRIC_DESCRIPTIONS
    )
    descs_for_run = [
        (m, description_table.get(m, "")) for m in metrics_in_run
        if description_table.get(m)
    ]
    if descs_for_run:
        lines.append("## What each metric means\n")
        for metric, desc in descs_for_run:
            pretty = metric.replace("_", " ").title()
            lines.append(f"- **{pretty}** — {desc}")
        lines.append("")

    # ── Juror panel — surface who scored this so the reader can
    # interpret the consensus debate that follows. Pulled from
    # report.metadata which is set by Harness._state_to_report.
    personas = report.metadata.get("personas") if report.metadata else None
    if personas:
        lines.append("## Juror panel\n")
        lines.append(
            f"Scored by **{len(personas)} jurors** "
            f"({'strict-artifact panel' if report.mode == 'artifact' else 'multi-turn panel'}): "
            f"{', '.join(personas)}.\n"
        )
        if report.mode == "artifact":
            lines.append(
                "Artifact-mode jurors are tuned for STRICT review of finished "
                "deliverables — each enforces a different lens (corpus "
                "traceability, decision-utility, adversarial reading) and "
                "default scores hover around 5-6/10, not 7-8. Scores ≥ 8 are "
                "deliberately rare and indicate an artifact worth approving as-is.\n"
            )

    # ── Per-turn audit — the forensic PASS / SOFT_FAIL / FAIL trail each
    #    juror produced BEFORE scoring (the harness's strongest signal). In
    #    BOTH modes: multi-turn = one row per turn; artifact = one row per
    #    major section/claim (turn_index 0). Final round shown.
    cl = report.consensus_log or {}
    is_artifact = report.mode == "artifact"
    audit_lines: list[str] = []
    for metric, cons in cl.items():
        rows = (getattr(cons, "round_two", None) or getattr(cons, "round_one", None) or [])
        metric_block: list[str] = []
        for js in rows:
            entries = getattr(js, "per_turn_audit", None) or []
            if not entries:
                continue
            metric_block.append(f"- **{getattr(js, 'persona', '?')}**")
            for e in entries:
                ti = getattr(e, "turn_index", "?")
                outcome = getattr(e, "outcome", "") or "?"
                cite = (getattr(e, "citation", "") or "").strip().replace("\n", " ")
                if len(cite) > 200:
                    cite = cite[:200] + "…"
                loc = f"item {ti}" if is_artifact else f"turn {ti}"
                metric_block.append(f"    - {loc} — `{outcome}`" + (f": {cite}" if cite else ""))
        if metric_block:
            audit_lines.append(f"### {metric}")
            audit_lines.extend(metric_block)
            audit_lines.append("")
    if audit_lines:
        audit_heading = (
            "Per-section audit (claim-by-claim forensic trail)" if is_artifact
            else "Per-turn audit (turn-by-turn forensic trail)"
        )
        lines.append(f"## {audit_heading}\n")
        lines.extend(audit_lines)

    if report.findings:
        lines.append("## Findings\n")
        for f in report.findings:
            lines.append(f"### {f.headline}")
            lines.append(f"- **Detail:** {f.detail}")
            if f.recommendation:
                lines.append(f"- **Recommendation:** {f.recommendation}")
            lines.append("")

    if getattr(report, "technical_issues", None):
        lines.append("## Technical issues\n")
        lines.append(
            "_Operational / behavioral anomalies observed during the eval "
            "(agent refusals, phantom / forbidden tool calls, crashes, "
            "harness LLM errors) — separate from the agent-quality findings._\n"
        )
        for f in report.technical_issues:
            sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
            lines.append(f"### [{sev.upper()}] {f.headline}")
            if f.metric:
                lines.append(f"- **Type:** `{f.metric}`")
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
