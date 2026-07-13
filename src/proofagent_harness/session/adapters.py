"""Capture adapters - turn a coding tool's native output into normalized
:class:`SessionEvent` s. The core is tool-agnostic; each adapter is a thin
mapping, so supporting a new tool is one function, not a new pipeline.

  * ``events-file`` - a JSONL stream any collector / hook emits (the universal path).
  * ``git``         - derive edit/write events from the working tree's diff
                      (works for Cursor / Copilot / Windsurf / anything that edits files).
  * ``claude-code`` - parse a Claude Code session transcript (tool_use blocks).

The default path is ``discover_session`` - the harness runs on the same machine
as the coding tool, so it *finds* the session instead of making the user pick a
file or a git flag: it locates the Claude Code transcript for the current
workspace, and falls back to the workspace ``git diff`` for file-editing tools
(Cursor / Copilot / Windsurf). An explicit source file or ``--from-git`` always
overrides discovery.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from proofagent_harness.session.events import SessionEvent, normalize_tool


def from_events_file(path: str | Path) -> list[SessionEvent]:
    """Load a normalized JSONL event stream (one event per line)."""
    events: list[SessionEvent] = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(SessionEvent.from_raw(raw, seq=i))
    return events


def _added_lines(unified_diff: str) -> str:
    """The ADDED lines of a unified diff - what the screen should inspect
    (a secret/insecure pattern the agent *introduced*, not pre-existing code)."""
    return "\n".join(
        ln[1:] for ln in unified_diff.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )


def from_git(workspace: str | Path, ref: str | None = None) -> list[SessionEvent]:
    """Derive write events from ``git diff`` in ``workspace`` - tool-agnostic:
    it doesn't matter which assistant made the edits."""
    ws = str(workspace)
    rng = [ref] if ref else []
    try:
        numstat = subprocess.run(
            ["git", "-C", ws, "diff", "--numstat", *rng],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    events: list[SessionEvent] = []
    for i, line in enumerate(numstat.splitlines()):
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        try:
            diff = subprocess.run(
                ["git", "-C", ws, "diff", *rng, "--", path],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            diff = ""
        events.append(SessionEvent(
            seq=i, kind="diff", tool="edit", action="write", target=path,
            added=int(added) if added.isdigit() else 0,
            removed=int(removed) if removed.isdigit() else 0,
            content=_added_lines(diff),
        ))
    return events


# Harness-injected blocks that ride inside a user message but are NOT the developer's
# intent — background-task events, system reminders, slash-command plumbing. Stripped
# so the intent trajectory reads as human prompts, not tool/system chatter.
_INJECTED_BLOCK = re.compile(
    r"<(system-reminder|task-notification|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr)\b[^>]*>.*?</\1>",
    re.S | re.I,
)


def _strip_injections(text: str) -> str:
    """Remove harness-injected blocks; keep the developer's actual words. A message
    that is ONLY an injection collapses to '' and is dropped as a non-intent."""
    return _INJECTED_BLOCK.sub("", text or "").strip()


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text", "")))
            elif isinstance(b, str):
                out.append(b)
        return " ".join(out)
    return ""


def from_claude_code(path: str | Path) -> list[SessionEvent]:
    """Parse a Claude Code session transcript (JSONL). User messages become
    ``prompt`` events (so pasted secrets/PII are screened); assistant
    ``tool_use`` blocks become tool events."""
    events: list[SessionEvent] = []
    seq = 0
    cwd = branch = version = first_ts = last_ts = ""
    tok_in = tok_out = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(rec.get("timestamp") or "")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            cwd = cwd or str(rec.get("cwd") or "")
            branch = branch or str(rec.get("gitBranch") or "")
            version = version or str(rec.get("version") or "")
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            out_tok = int(usage.get("output_tokens", 0) or 0)
            tok_out += out_tok
            tok_in += int(usage.get("input_tokens", 0) or 0)
            role = msg.get("role") or rec.get("type") or ""
            content = msg.get("content")
            if role == "user":
                # Claude Code sends tool RESULTS back as role="user" (content is
                # tool_result blocks). Those are plumbing, not the developer's intent —
                # only a message carrying real text is a prompt. Skipping the empty
                # (tool-result / image-only) ones is what makes the intent trajectory
                # the actual human turns, not thousands of tool echoes.
                text = _strip_injections(_blocks_text(content))
                if text:
                    events.append(SessionEvent(
                        seq=seq, ts=ts, kind="message", tool="", action="prompt",
                        content=text,
                    ))
                    seq += 1
            if isinstance(content, list):
                first_tool = True
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = str(b.get("name", ""))
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    tool, action = normalize_tool(name)
                    target = str(
                        inp.get("file_path") or inp.get("path") or inp.get("command")
                        or inp.get("url") or ""
                    )
                    body = str(
                        inp.get("content") or inp.get("new_string")
                        or inp.get("new_str") or inp.get("command") or ""
                    )
                    # MultiEdit / the text-editor tool carry the code in an
                    # `edits: [{old_string, new_string}, …]` array, not a scalar —
                    # join every new segment so secrets / CWE patterns are screened.
                    if not body and isinstance(inp.get("edits"), list):
                        body = "\n".join(
                            str(e.get("new_string") or e.get("new_str") or "")
                            for e in inp["edits"] if isinstance(e, dict)
                        )
                    events.append(SessionEvent(
                        seq=seq, ts=ts, kind="tool", tool=tool, action=action,
                        target=target, args=inp, content=body,
                        tokens=(out_tok if first_tool else 0),  # attribute the msg tokens once
                    ))
                    first_tool = False
                    seq += 1
    # A leading session_start carries the BOM context (repo/branch/tool version) and
    # the coding agent's OWN token spend for observability. action="other" + no
    # content, so it never triggers the risk screen or the access map.
    if events:
        events.insert(0, SessionEvent(
            seq=-1, ts=first_ts, kind="session_start", action="other",
            args={
                "cwd": cwd, "git_branch": branch, "tool_version": version,
                "first_ts": first_ts, "last_ts": last_ts,
                "agent_output_tokens": tok_out, "agent_input_tokens": tok_in,
            },
        ))
    return events


def _claude_projects_dir() -> Path:
    """Where Claude Code keeps per-project session transcripts. Overridable via
    ``PROOFAGENT_CLAUDE_PROJECTS_DIR`` (also makes discovery testable)."""
    env = os.environ.get("PROOFAGENT_CLAUDE_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def _claude_transcript_for(workspace: str | Path) -> Path | None:
    """The most recent Claude Code transcript for ``workspace``. Claude encodes
    the absolute cwd into the project-dir name by replacing every non-alphanumeric
    char with ``-`` (e.g. ``/Users/x/GitHub`` -> ``-Users-x-GitHub``)."""
    root = _claude_projects_dir()
    if not root.is_dir():
        return None
    ws = Path(workspace).resolve()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(ws))
    dirs = [root / slug] if (root / slug).is_dir() else []
    if not dirs:  # tolerate encoding drift across Claude versions: match by tail
        want = slug.lstrip("-")
        dirs = [d for d in root.iterdir() if d.is_dir() and d.name.lstrip("-") == want]
    files = [f for d in dirs for f in d.glob("*.jsonl")]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def discover_transcript_path(workspace: str | None) -> Path | None:
    """The live Claude Code transcript file for ``workspace`` (its filename is the
    Claude sessionId). Used by ``proof watch`` for the stable session_key + to detect
    new activity via mtime. Returns None for non-transcript tools."""
    return _claude_transcript_for(workspace or ".")


def discover_latest_transcript() -> Path | None:
    """The most recently ACTIVE Claude Code transcript across ALL projects — newest
    mtime wins. Lets ``proof watch`` run with NO ``--workspace`` and no ``cd``: from
    anywhere, it attaches to whatever session you're currently coding in. Zero paths."""
    root = _claude_projects_dir()
    if not root.is_dir():
        return None
    files = [f for d in root.iterdir() if d.is_dir() for f in d.glob("*.jsonl")]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def workspace_of_transcript(path: str | Path | None) -> str:
    """The repo a transcript belongs to, read from its records' ``cwd`` (Claude writes
    the session's working directory into every line). Lets a globally-discovered session
    be labelled + keyed by its real repo, not the folder proof happened to run from."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                cwd = str(json.loads(line).get("cwd") or "")
                if cwd:
                    return cwd
                break
    except (OSError, ValueError):
        pass
    return ""


def session_key_for(path: str | Path | None) -> str:
    """Stable session identity from a transcript path — Claude names the file after
    the sessionId, so the stem IS the session key."""
    return Path(path).stem if path else ""


def discover_session(
    *, tool: str, workspace: str | None
) -> tuple[list[SessionEvent], str]:
    """Find the live session with no file/format picking. Returns
    ``(events, resolved_tool)``. Order: Claude Code transcript for this workspace
    (when tool is ``auto``/``claude-code``), then the workspace ``git diff`` for
    any file-editing tool. ``resolved_tool`` reflects what was actually found."""
    ws = workspace or "."
    if tool in ("auto", "claude-code"):
        tp = _claude_transcript_for(ws)
        if tp is not None:
            events = from_claude_code(tp)
            if events:
                return events, "claude-code"
    events = from_git(ws)
    resolved = tool if tool not in ("auto", "claude-code") else ("generic" if events else tool)
    return events, resolved


def load_session(
    *,
    source: str | None,
    tool: str,
    workspace: str | None,
    from_git_flag: bool,
) -> tuple[list[SessionEvent], str]:
    """Dispatch to the right adapter and report the resolved tool. Precedence:
    an explicit ``source`` file, then ``--from-git``, then auto-discovery. Returns
    ``(events, resolved_tool)``."""
    if source:
        p = Path(source)
        if tool == "claude-code" or (p.suffix in (".jsonl",) and _looks_like_transcript(p)):
            return from_claude_code(p), ("claude-code" if tool == "auto" else tool)
        return from_events_file(p), (tool if tool != "auto" else "generic")
    if from_git_flag:
        return from_git(workspace or "."), (tool if tool != "auto" else "generic")
    return discover_session(tool=tool, workspace=workspace)


def _looks_like_transcript(path: Path) -> bool:
    """True if this JSONL is a Claude Code transcript. A real session interleaves
    non-message bookkeeping records (queue-operation, custom-title, mode, attachment…),
    so the FIRST line is usually NOT a message — checking only line 1 misreads a live
    session as a generic events file. Scan the head for any Claude message record."""
    try:
        with open(path, encoding="utf-8") as fh:
            for _ in range(500):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if "message" in rec or rec.get("type") in ("user", "assistant", "system"):
                    return True
    except OSError:
        return False
    return False
