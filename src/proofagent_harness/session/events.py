"""The normalized session event - the tool-agnostic unit every adapter emits.

A coding tool (Claude Code, Cursor, Copilot, Windsurf, a generic git watcher …)
produces very different raw logs; each adapter maps them onto this one shape so
the Tier-1 screen and the governance payload never need to know which tool ran.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Raw tool name (as a tool reports it)  ->  (canonical tool, canonical action).
# The canonical action is what the screen + access map key off, so a new tool
# only needs a row here, not new screen logic.
_TOOL_MAP: dict[str, tuple[str, str]] = {
    "read": ("read", "read"),
    "cat": ("read", "read"),
    "open": ("read", "read"),
    "grep": ("read", "read"),
    "glob": ("read", "read"),
    "ls": ("read", "read"),
    "edit": ("edit", "write"),
    "multiedit": ("edit", "write"),
    "write": ("write", "write"),
    "create": ("write", "write"),
    "notebookedit": ("edit", "write"),
    "applypatch": ("edit", "write"),
    "str_replace": ("edit", "write"),
    "bash": ("bash", "exec"),
    "shell": ("bash", "exec"),
    "run": ("bash", "exec"),
    "exec": ("bash", "exec"),
    "terminal": ("bash", "exec"),
    "webfetch": ("webfetch", "network"),
    "websearch": ("webfetch", "network"),
    "fetch": ("webfetch", "network"),
    "curl": ("webfetch", "network"),
}


def normalize_tool(name: str) -> tuple[str, str]:
    """Map a raw tool name onto (canonical_tool, canonical_action).

    MCP tools (``mcp__server__tool``) collapse to ``("mcp", "mcp")``. Unknown
    tools pass through as ``(lowered_name, "other")`` so nothing is dropped.
    """
    n = (name or "").strip().lower()
    if not n:
        return ("", "other")
    if n.startswith("mcp__") or n.startswith("mcp:"):
        return ("mcp", "mcp")
    return _TOOL_MAP.get(n, (n, "other"))


class SessionEvent(BaseModel):
    """One captured action inside a coding-agent session (tool-agnostic)."""

    model_config = ConfigDict(extra="allow")

    seq: int = 0
    ts: str = ""
    kind: str = "tool"          # tool | diff | message | network | session_start | session_end
    tool: str = ""              # canonical tool (read|edit|write|bash|webfetch|mcp|…)
    action: str = "other"       # canonical action (read|write|exec|network|mcp|prompt|other)
    target: str = ""            # file path | command | host
    args: dict = Field(default_factory=dict)
    added: int = 0              # lines added (diff/edit)
    removed: int = 0            # lines removed (diff/edit)
    content: str = ""           # optional snippet the screen inspects (redact upstream)
    host: str = ""              # for network events
    tokens: int = 0             # output tokens the coding agent spent on this step (observability)

    @classmethod
    def from_raw(cls, raw: dict, *, seq: int = 0) -> SessionEvent:
        """Build an event from a loose dict (an events-file line or an adapter),
        normalizing the tool/action and gathering text the screen can inspect."""
        tool_raw = str(raw.get("tool") or raw.get("name") or raw.get("type") or "")
        tool, action = normalize_tool(tool_raw)
        target = str(
            raw.get("target") or raw.get("path") or raw.get("file")
            or raw.get("command") or raw.get("cmd") or raw.get("url")
            or raw.get("host") or ""
        )
        # Text the deterministic screen scans: explicit content, else a flat dump
        # of args/command so secrets/PII pasted into a prompt or command are seen.
        content = str(raw.get("content") or raw.get("text") or raw.get("diff") or "")
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        if not content and args:
            content = " ".join(str(v) for v in args.values())
        return cls(
            seq=int(raw.get("seq", seq) or seq),
            ts=str(raw.get("ts") or raw.get("timestamp") or ""),
            kind=str(raw.get("kind") or ("tool" if tool else "message")),
            tool=tool,
            action=action,
            target=target,
            args=args,
            added=int(raw.get("added", 0) or 0),
            removed=int(raw.get("removed", 0) or 0),
            content=content,
            host=str(raw.get("host") or ""),
            tokens=int(raw.get("tokens", 0) or 0),
        )
