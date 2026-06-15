"""Log / event-stream converter — collapses noisy logs into compact
summaries that preserve auditable signal.

Real agent execution logs are huge (300K-1M chars) and 90% repetitive
boilerplate. For artifact-mode evaluation, the juror needs:
  * tool-call inventory (which tools, how many times, error rate)
  * event timeline (key milestones, not every heartbeat)
  * error highlights (anomalies the artifact may need to address)

Not the raw stream. This converter renders a structured summary the
juror can scan in seconds.

Handles two formats:
  * `.log` — text-formatted (line-per-event or block-per-event); we
    pattern-match common shapes (timestamp + level + message).
  * `.jsonl` / `.ndjson` — JSON-Lines event stream; parsed per-line.

In both cases, output is a markdown summary, NOT the raw log.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import ArtifactConversionError

# ─── .log files ───────────────────────────────────────────────────────────────

# Recognized log-line shapes (timestamp + level + message).
_TS_LEVEL_RE = re.compile(
    r"^\[?\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[\.,]?\d*)\]?\s+"
    r"(?:\[?(INFO|DEBUG|WARN|WARNING|ERROR|CRITICAL|TRACE|RAW_MESSAGE)\]?)?\s*",
    re.IGNORECASE,
)

# Heuristic for tool-call lines.
_TOOL_CALL_RE = re.compile(r"\b(?:tool|mcp|api)[\.:_-]?([a-z_][a-z0-9_]*)", re.IGNORECASE)


def read_log(p: Path) -> str:
    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to read log {p}: {type(exc).__name__}: {exc}"
        ) from exc

    lines = body.splitlines()
    n_lines = len(lines)
    level_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    errors: list[str] = []
    timestamps: list[str] = []
    first_ts: str | None = None
    last_ts: str | None = None

    for line in lines:
        m = _TS_LEVEL_RE.match(line)
        if m:
            ts, level = m.group(1), (m.group(2) or "INFO").upper()
            timestamps.append(ts)
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            level_counts[level] += 1
            if level in {"ERROR", "CRITICAL", "WARN", "WARNING"} and len(errors) < 20:
                errors.append(f"[{ts}] {level}: {line[m.end():m.end() + 200]}")
        # Tool-call inventory.
        for tm in _TOOL_CALL_RE.finditer(line):
            tool_counts[tm.group(1).lower()] += 1

    parts: list[str] = [f"# Log file summary: {p.name}\n"]
    parts.append("## Summary\n")
    parts.append(f"- **Total lines:** {n_lines:,}")
    parts.append(f"- **File size:** {p.stat().st_size:,} bytes")
    if first_ts and last_ts:
        parts.append(f"- **Time range:** {first_ts} → {last_ts}")
    if level_counts:
        levels_str = ", ".join(f"{k}={v}" for k, v in level_counts.most_common())
        parts.append(f"- **By level:** {levels_str}")
    parts.append("")

    if tool_counts:
        parts.append("## Tool calls observed\n")
        for tool, n in tool_counts.most_common(20):
            parts.append(f"- `{tool}`: {n} call(s)")
        if len(tool_counts) > 20:
            parts.append(f"- … and {len(tool_counts) - 20} more")
        parts.append("")

    if errors:
        parts.append("## Errors / warnings (first 20)\n")
        for e in errors:
            parts.append(f"- `{e[:240]}`")
        parts.append("")

    parts.append("## Notes\n")
    parts.append(
        "This is a SUMMARY of the log, not the raw stream. The juror "
        "should treat it as VERIFICATION evidence for claims made in the "
        "artifact (e.g., 'the agent invoked tool X 5 times' → check the "
        "Tool calls section above) — not as grounding corpus to score "
        "against."
    )
    return "\n".join(parts)


# ─── .jsonl / .ndjson files ──────────────────────────────────────────────────

def read_jsonl(p: Path) -> str:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to read jsonl {p}: {type(exc).__name__}: {exc}"
        ) from exc

    events: list[dict[str, Any]] = []
    parse_errors = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors += 1

    n_events = len(events)
    type_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    errors: list[str] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Common shape keys.
        ev_type = (
            ev.get("type") or ev.get("event_type") or ev.get("event")
            or ev.get("kind") or "unknown"
        )
        type_counts[str(ev_type)] += 1
        # Tool call inference.
        tool = (
            ev.get("tool") or ev.get("tool_name")
            or (ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}).get("tool_name")
            or (ev.get("name") if str(ev_type).startswith(("tool", "mcp")) else None)
        )
        if tool:
            tool_counts[str(tool)] += 1
        # Errors.
        if (
            str(ev_type).lower() in {"error", "exception"}
            or ev.get("error")
            or ev.get("level", "").lower() in {"error", "critical"}
        ) and len(errors) < 20:
            errors.append(json.dumps(ev)[:200])

    parts: list[str] = [f"# JSON-Lines event stream: {p.name}\n"]
    parts.append("## Summary\n")
    parts.append(f"- **Events parsed:** {n_events:,}")
    parts.append(f"- **File size:** {p.stat().st_size:,} bytes")
    if parse_errors:
        parts.append(f"- **Lines that failed to parse:** {parse_errors}")
    parts.append("")

    if type_counts:
        parts.append("## Event types\n")
        for t, n in type_counts.most_common(20):
            parts.append(f"- `{t}`: {n}")
        if len(type_counts) > 20:
            parts.append(f"- … and {len(type_counts) - 20} more")
        parts.append("")

    if tool_counts:
        parts.append("## Tool invocations\n")
        for t, n in tool_counts.most_common(20):
            parts.append(f"- `{t}`: {n}")
        parts.append("")

    if errors:
        parts.append("## Error events (first 20)\n")
        for e in errors:
            parts.append(f"- `{e}`")
        parts.append("")

    parts.append("## Notes\n")
    parts.append(
        "Summary, not raw. Use this as VERIFICATION evidence for claims "
        "in the artifact (e.g., 'invoked MCP tool X' → check Tool "
        "invocations). The artifact is scored against the corpus, not "
        "against this stream."
    )
    return "\n".join(parts)
