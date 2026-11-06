"""The target-agent brief: what the attacker needs to know to attack THIS agent rather than a generic one.

Built from the run's context — role, goal and business case, the system prompt, the tool schemas, and the rules in
the domain knowledge — and shared by the planner (domain inference, agent-specific attacks) and the conductor (every
turn). The trap library supplies the technique; the brief supplies the target.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Lines of the knowledge that state a rule: a limit, a threshold, an obligation or a prohibition.
_RULE = re.compile(
    r"\d|\b(must|must not|never|always|only|prohibit\w*|forbid\w*|not (?:allowed|permitted)|require\w*|may not|"
    r"limit\w*|threshold|maximum|minimum|cap\w*|eligib\w*|exception|escalat\w*|approv\w*|declin\w*|adverse|"
    r"consent|verify|verification|disclos\w*)\b", re.I)


def _tools(state: Any) -> list[dict[str, Any]]:
    ctx = state.get("context")
    return [t for t in (getattr(ctx, "tools", None) or []) if isinstance(t, dict)]


def tool_lines(state: Any, limit: int = 12) -> list[str]:
    """One line per tool: its name, what it does, and its arguments."""
    out = []
    for spec in _tools(state)[:limit]:
        fn = spec.get("function") if isinstance(spec.get("function"), dict) else spec
        name = fn.get("name") or spec.get("name")
        if not name:
            continue
        desc = " ".join(str(fn.get("description") or "").split())[:160]
        params = fn.get("parameters") or fn.get("input_schema") or {}
        props = list((params.get("properties") or {}).keys()) if isinstance(params, dict) else []
        out.append(f"- {name}({', '.join(props[:8])}): {desc}".rstrip(": "))
    return out


def knowledge_rules(state: Any, limit: int = 2200) -> str:
    """The rule-bearing lines of the domain knowledge (limits, thresholds, obligations, prohibitions)."""
    text = str(state.get("knowledge_text") or "")
    lines, seen, size = [], set(), 0
    for raw in text.splitlines():
        line = " ".join(raw.strip().lstrip("-*#>").split())
        if len(line) < 18 or line in seen or not _RULE.search(line):
            continue
        seen.add(line)
        lines.append(f"- {line[:240]}")
        size += len(lines[-1])
        if size > limit:
            break
    return "\n".join(lines)


def agent_brief(state: Any, *, prompt_chars: int = 2500) -> str:
    """The brief, as a block for a prompt. Empty parts are left out."""
    ctx = state.get("context")
    sp = " ".join(str(getattr(ctx, "system_prompt", "") or "").split())
    parts = [
        f"Role: {state.get('role') or '-'}",
        f"Goal: {state.get('goal') or '-'}",
        f"Business case: {state.get('business_case') or '-'}",
    ]
    if sp:
        parts.append(f"System prompt (excerpt): {sp[:prompt_chars]}")
    tl = tool_lines(state)
    if tl:
        parts.append("Tools:\n" + "\n".join(tl))
    rules = knowledge_rules(state)
    if rules:
        parts.append("Rules from its domain knowledge (limits, thresholds, obligations, prohibitions):\n" + rules)
    return "\n".join(parts)


def brief_json(state: Any) -> str:
    """The brief's facts as compact JSON, for reports and tests."""
    return json.dumps({"tools": tool_lines(state), "rules": knowledge_rules(state).splitlines()}, ensure_ascii=False)
