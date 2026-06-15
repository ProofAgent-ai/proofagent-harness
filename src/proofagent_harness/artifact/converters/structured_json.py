"""Structured-JSON converter — detects common agent-output shapes and
renders them juror-friendly.

The biggest pattern in the wild: an agent that emits a "decisions list"
({decisions: [{id, category, recommended, selected, options, justification}]}).
Read as raw JSON text, the juror sees the structure but loses scanning
efficiency. This converter renders such shapes as:

    Decision: <category>
    Recommended: <recommended>
    Selected: <selected or "NONE YET">
    Options: <comma-separated>
    Why: <justification>

Other JSON shapes fall through to pretty-printed text with light
annotation (depth, root keys).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import ArtifactConversionError


def read_structured_json(p: Path) -> str:
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to parse JSON {p}: {type(exc).__name__}: {exc}"
        ) from exc

    # Specialised renderers for known shapes — these produce the most
    # useful juror prompt content.
    if isinstance(data, dict):
        # Decision-list pattern (e.g. engineering_plan.json from MARS).
        if isinstance(data.get("decisions"), list) and data["decisions"]:
            return _render_decision_list(p, data)
        # Test-results pattern: {tests: [{name, status, …}]}
        if isinstance(data.get("tests"), list):
            return _render_test_results(p, data)
        # Generic dict — render with a key tree summary.
        return _render_generic_dict(p, data, raw)

    if isinstance(data, list):
        return _render_generic_list(p, data, raw)

    # Primitive — just dump.
    return f"# JSON artifact: {p.name}\n\n{raw}"


# ─── Decision list ────────────────────────────────────────────────────────────

def _render_decision_list(p: Path, data: dict[str, Any]) -> str:
    parts: list[str] = [f"# Engineering decisions: {p.name}\n"]

    # Surface top-level metadata (status, project, etc.).
    meta_keys = [k for k in ("status", "project", "workspace", "app_name") if k in data]
    if meta_keys:
        parts.append("## Document metadata\n")
        for k in meta_keys:
            parts.append(f"- **{k}**: `{data[k]}`")
        parts.append("")

    # Decisions, one section each. The selected vs recommended distinction
    # is the most important signal — surface it prominently.
    parts.append("## Decisions\n")
    for d in data["decisions"]:
        if not isinstance(d, dict):
            continue
        d_id = d.get("id", "?")
        category = d.get("category", d_id)
        recommended = d.get("recommended", "—")
        selected = d.get("selected")
        selected_str = "NONE YET (pending decision)" if selected is None else str(selected)
        options = d.get("options", [])
        options_str = _format_options(options)
        justification = (d.get("justification", "") or "").strip()

        parts.append(f"### {category} (`{d_id}`)")
        parts.append(f"- **Recommended:** {recommended}")
        parts.append(f"- **Selected:**    {selected_str}")
        if options_str:
            parts.append(f"- **Options:**     {options_str}")
        if justification:
            parts.append(f"- **Why:** {justification}")
        parts.append("")

    # Trailing flat metadata (exact_model_strings, apps_to_provision).
    extras = {k: v for k, v in data.items() if k not in {"decisions", *meta_keys}}
    if extras:
        parts.append("## Other fields\n")
        parts.append("```json")
        parts.append(json.dumps(extras, indent=2)[:8000])
        parts.append("```")

    return "\n".join(parts)


def _format_options(options: Any) -> str:
    if not options:
        return ""
    if isinstance(options, list):
        out: list[str] = []
        for o in options:
            if isinstance(o, dict):
                name = o.get("name", "")
                notes = o.get("notes", "")
                out.append(f"{name}" + (f" ({notes})" if notes else ""))
            else:
                out.append(str(o))
        return " · ".join(out)
    return str(options)


# ─── Test results ─────────────────────────────────────────────────────────────

def _render_test_results(p: Path, data: dict[str, Any]) -> str:
    parts: list[str] = [f"# Test results: {p.name}\n"]
    summary = data.get("summary") or {}
    if summary:
        parts.append("## Summary\n")
        for k, v in summary.items():
            parts.append(f"- **{k}**: `{v}`")
        parts.append("")
    parts.append("## Tests\n")
    for t in data["tests"]:
        if not isinstance(t, dict):
            continue
        name = t.get("name", "?")
        status = t.get("status", "?")
        duration = t.get("duration_ms")
        line = f"- **{status}** — {name}"
        if duration is not None:
            line += f" ({duration} ms)"
        parts.append(line)
        for k in ("error", "message", "stack"):
            v = t.get(k)
            if v:
                parts.append(f"  - {k}: `{str(v)[:200]}`")
    return "\n".join(parts)


# ─── Generic fallback ─────────────────────────────────────────────────────────

def _render_generic_dict(p: Path, data: dict[str, Any], raw: str) -> str:
    parts: list[str] = [
        f"# JSON artifact: {p.name}\n",
        "## Top-level keys\n",
    ]
    for k, v in data.items():
        vt = type(v).__name__
        size = len(v) if isinstance(v, (list, dict, str)) else "—"
        parts.append(f"- `{k}` ({vt}, size {size})")
    parts.append("")
    parts.append("## Pretty-printed content\n")
    parts.append("```json")
    parts.append(raw[:30_000])
    if len(raw) > 30_000:
        parts.append(f"... [{len(raw) - 30_000:,} chars truncated]")
    parts.append("```")
    return "\n".join(parts)


def _render_generic_list(p: Path, data: list[Any], raw: str) -> str:
    parts: list[str] = [
        f"# JSON list artifact: {p.name}\n",
        "## Summary\n",
        f"- {len(data)} top-level entries",
        f"- entry types: {sorted({type(x).__name__ for x in data[:50]})}",
        "",
        "## Pretty-printed content\n",
        "```json",
        raw[:30_000],
    ]
    if len(raw) > 30_000:
        parts.append(f"... [{len(raw) - 30_000:,} chars truncated]")
    parts.append("```")
    return "\n".join(parts)
