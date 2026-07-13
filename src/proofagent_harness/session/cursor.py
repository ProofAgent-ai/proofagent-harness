"""Cursor capture adapter.

Cursor is a VS Code fork; unlike Claude Code it writes no JSONL transcript. Its AI chats
live in a per-workspace SQLite DB (``…/Cursor/User/workspaceStorage/<hash>/state.vscdb``),
under ``ItemTable`` key ``aiService.prompts`` — a JSON array of the developer's prompts.

So we read the PROMPTS (intents) from that DB and merge the workspace ``git diff`` for the
edits Cursor made, giving a Cursor session a real intent trajectory PLUS screenable code —
not just a bare diff. Cursor does not expose per-turn token counts, so tokens read as 0.

``workspace.json`` in each storage dir carries the repo it belongs to (``folder`` URI), so
we can find the right DB for a given repo. The DB is opened read-only, safe while Cursor runs.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

from proofagent_harness.session.events import SessionEvent


def _cursor_storage_dir() -> Path:
    """Where Cursor keeps per-workspace state DBs. Overridable via
    ``PROOFAGENT_CURSOR_STORAGE_DIR`` (also makes discovery testable)."""
    env = os.environ.get("PROOFAGENT_CURSOR_STORAGE_DIR")
    if env:
        return Path(env)
    # macOS default. (Linux: ~/.config/Cursor/…, Windows: %APPDATA%/Cursor/… — add as needed.)
    return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"


def _folder_of(ws_json: Path) -> str:
    """The repo path a storage dir belongs to, decoded from its workspace.json folder URI."""
    try:
        folder = json.loads(ws_json.read_text(encoding="utf-8")).get("folder", "")
    except (OSError, ValueError):
        return ""
    if folder.startswith("file://"):
        return unquote(urlparse(folder).path)
    return folder


def discover_cursor_db(workspace: str | Path) -> Path | None:
    """The Cursor ``state.vscdb`` whose workspace folder matches ``workspace`` (newest by
    mtime if several). Returns None when Cursor isn't installed or hasn't seen this repo."""
    root = _cursor_storage_dir()
    if not root.is_dir():
        return None
    want = str(Path(workspace).resolve())
    hits: list[Path] = []
    for d in root.iterdir():
        db = d / "state.vscdb"
        if not db.is_file():
            continue
        folder = _folder_of(d / "workspace.json")
        if folder and str(Path(folder).resolve()) == want:
            hits.append(db)
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def cursor_session_key(db: str | Path) -> str:
    """Stable session identity for a Cursor workspace — the storage-dir hash."""
    return f"cursor:{Path(db).parent.name}"


def _read_json_key(db: Path, key: str):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            row = con.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def from_cursor(db: str | Path, workspace: str | Path | None = None) -> list[SessionEvent]:
    """Parse a Cursor workspace DB into events: prompts → ``prompt`` (intent) events,
    merged with the workspace ``git diff`` write events so introduced secrets / CWE
    patterns are screened. A leading ``session_start`` carries Cursor's edit stats."""
    # Lazy import to avoid a cycle (adapters imports this module).
    from proofagent_harness.session.adapters import _strip_injections, from_git

    db = Path(db)
    events: list[SessionEvent] = []
    seq = 0
    for p in _read_json_key(db, "aiService.prompts") or []:
        if not isinstance(p, dict):
            continue
        text = _strip_injections(str(p.get("text") or ""))
        if text:
            events.append(SessionEvent(
                seq=seq, kind="message", tool="", action="prompt", content=text))
            seq += 1
    if workspace:  # the edits Cursor actually made — tool-agnostic, from the working tree
        for e in from_git(workspace):
            e.seq = seq
            seq += 1
            events.append(e)

    cdata = _read_json_key(db, "composer.composerData") or {}
    composers = cdata.get("allComposers") if isinstance(cdata, dict) else []
    composers = [c for c in composers if isinstance(c, dict)]
    if events:
        events.insert(0, SessionEvent(
            seq=-1, kind="session_start", action="other",
            args={
                "cwd": str(workspace or ""), "tool_version": "cursor",
                "composers": len(composers),
                "files_changed": sum(int(c.get("filesChangedCount") or 0) for c in composers),
                "lines_added": sum(int(c.get("totalLinesAdded") or 0) for c in composers),
                "lines_removed": sum(int(c.get("totalLinesRemoved") or 0) for c in composers),
                "agent_output_tokens": 0, "agent_input_tokens": 0,  # Cursor exposes no token counts
            }))
    return events
