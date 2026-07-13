"""Registry of the coding tools ``proof watch`` can govern.

Two facts per tool, and they answer the two questions a user asks:

  * ``name``  — the friendly display name, used as the DEFAULT governance agent so a
    session reports to the right agent on the portal (attribution is by exact name:
    a run tagged "Claude Code" attaches to the "Claude Code" agent, creating it once).
  * ``where`` / ``discovery`` — WHERE the tool drops its session and HOW we read it.
    Claude Code writes a readable per-project JSONL transcript, so we parse it directly.
    Cursor / Copilot / Windsurf don't expose one the same way, so we fall back to the
    workspace ``git diff`` — tool-agnostic, since it doesn't matter who made the edits.

Adding a tool with a readable session log is a new entry here plus one adapter function.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodingTool:
    slug: str        # canonical id carried on the wire (payload.tool)
    name: str        # friendly display + DEFAULT governance agent name
    where: str       # where this tool drops its session (for humans)
    discovery: str   # "transcript" (readable log we parse) | "git" (working-tree diff)


REGISTRY: dict[str, CodingTool] = {
    "claude-code": CodingTool(
        "claude-code", "Claude Code",
        "~/.claude/projects/<encoded-cwd>/<session-id>.jsonl", "transcript"),
    "cursor": CodingTool(
        "cursor", "Cursor",
        "~/…/Cursor/User/workspaceStorage/<hash>/state.vscdb (SQLite)", "cursor-db"),
    "copilot": CodingTool(
        "copilot", "GitHub Copilot", "workspace git diff", "git"),
    "windsurf": CodingTool(
        "windsurf", "Windsurf", "workspace git diff", "git"),
    "aider": CodingTool(
        "aider", "Aider", "workspace git diff", "git"),
    "generic": CodingTool(
        "generic", "Coding agent", "workspace git diff", "git"),
}


def resolve(slug: str) -> CodingTool:
    """The registered tool for ``slug``, or a sensible ad-hoc entry for an unknown one
    (title-cased name, git discovery) so nothing is ever dropped or mis-attributed."""
    s = (slug or "").strip().lower()
    if s in REGISTRY:
        return REGISTRY[s]
    return CodingTool(
        s or "generic", (s or "coding agent").replace("-", " ").title(),
        "workspace git diff", "git")


def display_name(slug: str) -> str:
    """The friendly agent name for a tool slug, e.g. 'claude-code' -> 'Claude Code'."""
    return resolve(slug).name
