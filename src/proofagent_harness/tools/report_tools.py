"""Render Reports to terminal (Rich), Markdown, and HTML."""

from __future__ import annotations

import contextlib
from typing import Any

from rich import box
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


def pct(score_out_of_10: float | None) -> str:
    """Render a 0-10 score as a percentage — the ONE display convention.

    Every user-facing number reads out of 100 (final score, per-metric, context
    engineering, compliance, PAI), so nothing has to be mentally rescaled and a
    metric can be compared to an axis at a glance. Display only: the stored values
    stay on their native 0-10 scale, which is the report/upload contract."""
    if score_out_of_10 is None:
        return "—"
    return f"{round(float(score_out_of_10) * 10)}%"


def _score_display(report: Report) -> str:
    """The score string — '—' for an INCOMPLETE run (nothing was scored), so a
    placeholder 0.0 is never shown as if it were an agent grade."""
    if report.certification == Certification.INCOMPLETE:
        return "— (not scored)"
    return pct(report.final_score)


def _finding_body_lines(f: Any) -> list[str]:
    """Render a Finding as concise Problem / Proof / Fix bullets — falling back to
    the legacy detail/recommendation strings for older findings."""
    out: list[str] = []
    problems = list(getattr(f, "problem", None) or [])
    if not problems and getattr(f, "detail", ""):
        problems = [f.detail]
    fixes = list(getattr(f, "fix", None) or [])
    if not fixes and getattr(f, "recommendation", ""):
        fixes = [f.recommendation]
    proof = getattr(f, "proof", "") or ""
    if len(problems) == 1:
        out.append(f"- **Problem:** {problems[0]}")
    elif problems:
        out.append("- **Problem:**")
        out.extend(f"    - {p}" for p in problems)
    if proof:
        out.append(f"- **Proof:** {proof}")
    if len(fixes) == 1:
        out.append(f"- **Fix:** {fixes[0]}")
    elif fixes:
        out.append("- **Fix:**")
        out.extend(f"    - {fx}" for fx in fixes)
    return out

_AXIS_ROWS = (
    ("evaluation", "E"),
    ("context", "Q"),
    ("compliance", "C"),
    ("governance", "G"),
)


def render_axes(report: Report) -> Any | None:
    """One table over every CONFIGURED axis: the axis score, then its components.

    Numbers only — an axis with no evidence is omitted rather than shown as zero, and
    the per-finding narrative stays in the markdown report. Returns None when the run
    carries no index (nothing to decompose)."""
    from proofagent_harness.scoring.pai import severity_for

    pai = getattr(report, "pai", None) or {}
    axes = {a.get("key"): a for a in (pai.get("axes") or [])}
    if not axes:
        return None

    # CONFIDENCE IS A REPORTED COLUMN, not an internal number. Measured across 15 runs in
    # three domains, it predicted reproducibility every time: metrics at >=0.95 replayed
    # byte-exact, while those at 0.82-0.90 moved 2.7 to 9.2 pp on an IDENTICAL transcript.
    # It is the per-metric answer to "how much should I trust this number", so a reader
    # comparing two runs needs it beside the score rather than buried in the JSON.
    confidence = dict(getattr(report, "confidence", None) or {})

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold", pad_edge=False)
    table.add_column("", width=2)
    table.add_column("Axis / metric", no_wrap=True)
    table.add_column("Score", justify="right", width=7)
    table.add_column("Severity", justify="left", width=9)
    table.add_column("Conf.", justify="right", width=6)

    first = True
    for key, sym in _AXIS_ROWS:
        a = axes.get(key)
        if a is None or not a.get("present"):
            continue
        if not first:
            table.add_row("", "", "", "", "", "")
        first = False
        asev = severity_for(a.get("score"))
        # Axis-level confidence is the WORST of its metrics: one unstable metric is
        # enough to make the axis unstable, so an average would hide it.
        axis_conf = min(confidence.values(), default=None) if key == "evaluation" else None
        table.add_row(
            Text(sym, style="bold cyan"),
            Text(str(a.get("label") or key), style="bold"),
            Text(_pct100(a.get("score")), style="bold"),
            Text(asev, style=f"bold {_SEV_STYLES.get(_sev_enum(asev), '')}"),
            Text(_conf(axis_conf), style=f"bold {_conf_style(axis_conf)}"),
        )
        for s in (a.get("sub") or []):
            sev = str(s.get("severity") or "")
            c = confidence.get(_metric_key(s.get("name", ""))) if key == "evaluation" else None
            table.add_row(
                "",
                f"  {s.get('name', '')}",
                _pct100(s.get("score")) if s.get("score") is not None else s.get("coverage", "-"),
                Text(sev, style=_SEV_STYLES.get(_sev_enum(sev), "")) if sev else "",
                Text(_conf(c), style=_conf_style(c)),
            )
    return table


def _metric_key(display_name: str) -> str:
    """"Task Success" -> "task_success", so a rendered row can find its confidence."""
    return display_name.strip().lower().replace(" ", "_")


def _conf(v: Any) -> str:
    return f"{float(v):.2f}" if isinstance(v, (int, float)) else ""


def _conf_style(v: Any) -> str:
    """Flag the metrics whose numbers moved on a replay in the validation runs.

    The 0.95 boundary is where the measured behaviour changed, not a guess: above it
    every metric reproduced exactly; below it every one moved.
    """
    if not isinstance(v, (int, float)):
        return ""
    if v >= 0.95:
        return "green"
    return "yellow" if v >= 0.85 else "red"


def _pct100(v: Any) -> str:
    return f"{float(v):.0f}%" if isinstance(v, (int, float)) else "-"


def _sev_enum(name: str) -> Any:
    with contextlib.suppress(Exception):
        return Severity(name)
    return None


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
            pct(score),
            f"{round(conf * 100)}%",
            Text(sev.value, style=_SEV_STYLES.get(sev, "")),
        )

    cert_text = Text(
        report.certification.value,
        style=_CERT_STYLES.get(report.certification, "white"),
    )

    # TOTAL tokens = the harness LLM's own spend PLUS the agent under test's, when the
    # agent exposed usage. Reporting only the harness half understates what the run cost.
    _agent_tokens = 0
    _perf = getattr(report, "performance", None) or {}
    if isinstance(_perf, dict):
        _agent_tokens = int((_perf.get("tokens") or {}).get("total_tokens") or 0)
    _total_tokens = int(report.tokens_used or 0) + _agent_tokens

    cert_line = Text()
    cert_line.append("Certification: ", style="bold")
    cert_line.append(cert_text)
    cert_line.append(f"    Tokens: {_total_tokens:,}", style="dim")
    if _agent_tokens:
        cert_line.append(
            f" (harness {report.tokens_used:,} · agent {_agent_tokens:,})", style="dim",
        )
    # Harness-LLM spend — the number a corporate buyer asks about first.
    _harness_cost = float(report.primary_cost_usd or 0.0) + float(report.fallback_cost_usd or 0.0)
    if _harness_cost > 0:
        cert_line.append(f"    Harness LLM cost: ${_harness_cost:.4f}", style="dim")

    # The axis decomposition SUPERSEDES the flat scorecard when the run carries an
    # index: E already reports the final score, so showing both is duplication.
    # Falls back to the scorecard for runs with no index (e.g. artifact mode).
    _axis_table = render_axes(report)
    blocks: list[Any] = [_axis_table or table, Text(""), cert_line]

    # ProofAgent Index — one readiness line, so the Python API sees the same verdict
    # the CLI prints. Compact by design: the full decomposition is on report.pai and
    # in the Markdown report.
    _pai = getattr(report, "pai", None) or {}
    if isinstance(_pai, dict) and _pai.get("score") is not None:
        _tone = {"good": "green", "warn": "yellow", "bad": "red"}.get(_pai.get("tone"), "white")
        _vcolor = "red" if (_pai.get("blocked") or _pai.get("readiness") == "not_ready") else (
            "yellow" if _pai.get("readiness") in ("indeterminate", "ready_with_caveats")
            else "green"
        )
        pai_line = Text()
        pai_line.append("PAI", style="bold")
        pai_line.append(" (ProofAgent Governance Readiness Index)  ", style="dim")
        _m = _pai.get("margin")
        _sc = f"{_pai['score']} +/- {_m}" if _m else f"{_pai['score']}"
        pai_line.append(f"{_sc} / 100   ", style=f"bold {_tone}")
        pai_line.append(f"{_pai.get('grade', '')} · {_pai.get('band', '')}   ", style=_tone)
        pai_line.append(str(_pai.get("verdict", "")), style=f"bold {_vcolor}")
        pai_line.append(f"   ({_pai.get('completeness', '')})", style="dim")
        blocks.append(pai_line)
        # Say WHAT capped the score, not just that it was capped. `reasons` mixes hard
        # blocks with a below-bar gate decision and a withheld compliance axis; read as a
        # flat list beside "BLOCKED" they all look responsible, and the governance line in
        # particular invites the wrong conclusion \u2014 a strict profile lowers G and is
        # surfaced, but never caps.
        _caps = [r for r in (_pai.get("cap_reasons") or []) if r]
        if _pai.get("blocked") and _pai.get("raw_score") != _pai.get("score"):
            _why = ("; ".join(c.rstrip(".") for c in _caps) if _caps
                    else "hard block (reason not recorded)")
            blocks.append(Text(
                f"  uncapped {_pai.get('raw_score')} \u2192 capped to {_pai.get('score')} "
                f"by: {_why}", style="dim"))
        for _r in (_pai.get("reasons") or []):
            _is_cap = _r in _caps
            blocks.append(Text(
                f"  \u2022 {_r}" + ("" if _is_cap else "  (does not cap)"),
                style="yellow" if _is_cap else "dim"))

    if _axis_table is None:
        sev_line = _build_severity_summary_line(report)
        if sev_line is not None:
            blocks.append(sev_line)
        next_step = _next_step_hint(report)
        if next_step is not None:
            blocks.append(Text(""))
            blocks.append(Text(f"Next: {next_step}", style="bold cyan"))

    if report.warnings:
        blocks.append(Text(""))
        for w in report.warnings:
            blocks.append(Text(f"  ! {_first_sentence(w)}", style="yellow"))

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
    _harness_cost = float(report.primary_cost_usd or 0.0) + float(report.fallback_cost_usd or 0.0)
    if _harness_cost > 0:
        lines.append(f"**Harness LLM cost:** `${_harness_cost:.4f}`  ")
    lines.append(f"**Duration:** `{report.duration_seconds:.1f}s`\n")

    lines.append(f"> {report.summary}\n")

    if report.warnings:
        lines.append("## Warnings\n")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    # ── ProofAgent Index (PAI) — the readiness verdict over all four axes ──
    _pai = getattr(report, "pai", None) or {}
    if isinstance(_pai, dict) and _pai.get("score") is not None:
        _m = _pai.get("margin")
        _shown = f"{_pai.get('score')} ± {_m}" if _m else f"{_pai.get('score')}"
        lines.append(
            f"## PAI — ProofAgent Governance Readiness Index — {_shown}/100 "
            f"({_pai.get('grade', '')} · {_pai.get('band', '')})\n"
        )
        lines.append(f"**Verdict:** `{_pai.get('verdict', '')}`  ")
        lines.append(f"**Completeness:** `{_pai.get('completeness', '')}`  ")
        _caps = [r for r in (_pai.get("cap_reasons") or []) if r]
        if _pai.get("blocked") and _pai.get("raw_score") != _pai.get("score"):
            _why = ("; ".join(c.rstrip(".") for c in _caps) if _caps
                    else "hard block (reason not recorded)")
            lines.append(
                f"**Uncapped (PAI_raw):** `{_pai.get('raw_score')}` — capped to "
                f"`{_pai.get('score')}` by: {_why}  "
            )
        lines.append("")
        lines.append("| Axis | Score | Weight |")
        lines.append("|---|---|---|")
        for a in _pai.get("axes") or []:
            # Axes are already 0-100, so they render as a percentage like every
            # other score in this report.
            _sc = f"{a.get('score')}%" if a.get("present") else "not measured"
            _weak = "  ← weakest" if a.get("key") == _pai.get("weakest_axis") else ""
            lines.append(
                f"| {a.get('symbol', '')} — {a.get('label', '')} | {_sc}{_weak} "
                f"| {a.get('weight')} |"
            )
        lines.append("")
        for r in _pai.get("reasons") or []:
            lines.append(f"- {r}" + ("" if r in _caps else " _(does not cap the index)_"))
        if _pai.get("reasons"):
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
            _noncompliant = []
            for c in fw.get("controls", []):
                ref = f"{c.get('ref', '')} {c.get('title', '')}".strip()
                lines.append(f"| {ref} | {c.get('status', '')} | {c.get('rationale', '')} |")
                if c.get("problem") or c.get("fix"):
                    _noncompliant.append((ref, c))
            lines.append("")
            # Why-not-compliant / proof / fix for the controls that need attention.
            for ref, c in _noncompliant:
                lines.append(f"**{ref} — {c.get('status', '')}**")
                for p in (c.get("problem") or []):
                    lines.append(f"- Problem: {p}")
                if c.get("proof"):
                    lines.append(f"- Proof: {c['proof']}")
                for fx in (c.get("fix") or []):
                    lines.append(f"- Fix: {fx}")
                lines.append("")

    # ── Context engineering (optional, reporter-generated) ───────────
    _ce = getattr(report, "context_engineering", None) or {}
    if isinstance(_ce, dict) and _ce.get("generated"):
        _impact = {"big_cut": "↓↓", "cut": "↓", "neutral": "→", "adds": "↑"}
        _savings = int(_ce.get("token_savings_estimate") or 0)
        _ce_score = _ce.get("score")
        _ce_pct = f"{round(_ce_score * 10)}%" if isinstance(_ce_score, (int, float)) else "?"
        head = f"## Context engineering — {_ce_pct} ({_ce.get('grade', '')})"
        if _savings:
            head += f" · ~{_savings:,} tokens reclaimable"
            _sv_pct = _ce.get("token_savings_pct")
            _ctx_tok = int(_ce.get("context_tokens") or 0)
            if _sv_pct is not None and _ctx_tok:
                head += f" ({_sv_pct}% of the {_ctx_tok:,}-token context)"
        lines.append(head)
        if _ce.get("summary"):
            lines.append(f"_{_ce['summary']}_\n")
        _subs = _ce.get("sub_criteria") or []
        if _subs:
            lines.append("| Criterion | Score |")
            lines.append("|---|---|")
            for s in _subs:
                _s = s.get("score")
                _s_pct = f"{round(_s * 10)}%" if isinstance(_s, (int, float)) else "?"
                lines.append(f"| {s.get('name', s.get('id', ''))} | {_s_pct} |")
            lines.append("")
        _cf = _ce.get("findings") or []
        if _cf:
            lines.append("| Finding | Proof | Fix | Tokens |")
            lines.append("|---|---|---|---|")
            for f in _cf:
                _proof = str(f.get("proof", "")).replace("|", "\\|")
                lines.append(
                    f"| {f.get('title', '')} — {f.get('problem', '')} "
                    f"| {('“' + _proof + '”') if _proof else '—'} "
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
            f"| {pretty} | {pct(score)} | {round(conf * 100)}% | {sev} |"
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
            lines.extend(_finding_body_lines(f))
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
            lines.extend(_finding_body_lines(f))
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
