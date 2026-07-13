"""The LOCAL ProofAgent session — a durable, on-disk accumulator that ``proof watch``
appends to on every Tier-1 tick.

This is the file the interval LLM synthesis reads from, and it survives restarts. It
holds three things, refreshed every scan (Tier-1, 0 tokens):

  * ``global_tokens`` — the agent's FULL consumption, tallied whether or not any risk
    was found (a separate counter, always advancing);
  * ``intents``       — every real developer prompt with the tokens that turn consumed;
  * ``flagged``       — only the turns where Tier-1 flagged a risk, each with the risk +
    redacted proof, the tools that ran, tokens, and a timestamp.

It NEVER leaves the machine — only the synthesized trajectory built from it is uploaded.
Because a Claude Code transcript is append-only, a full re-screen each tick IS the current
accumulated state, so ``refresh`` recomputes rather than trying to diff.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from proofagent_harness.session.runner import SessionResult, _intent_preview


def _state_dir(override: str | None = None) -> Path:
    base = Path(override) if override else Path.home() / ".proofagent" / "live"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_name(session_key: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in session_key) or "session"


@dataclass
class LiveSession:
    """The on-disk local session accumulator. One file per ``session_key``."""

    session_key: str
    workspace: str = ""
    tool: str = ""
    # Global token counter — advances every tick, risk or no risk.
    global_tokens: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "total": 0})
    intents: list[dict[str, Any]] = field(default_factory=list)
    flagged: list[dict[str, Any]] = field(default_factory=list)
    scans: int = 0
    last_scan: str = ""

    @classmethod
    def path_for(cls, session_key: str, *, state_dir: str | None = None) -> Path:
        return _state_dir(state_dir) / f"{_safe_name(session_key)}.json"

    @classmethod
    def load_or_new(
        cls, session_key: str, *, workspace: str = "", tool: str = "",
        state_dir: str | None = None,
    ) -> LiveSession:
        p = cls.path_for(session_key, state_dir=state_dir)
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                known = set(cls.__dataclass_fields__)
                return cls(**{k: v for k, v in d.items() if k in known})
            except (OSError, ValueError, TypeError):
                pass  # corrupt/stale file — start fresh, never crash the watch
        return cls(session_key=session_key, workspace=workspace, tool=tool)

    def refresh(self, result: SessionResult, *, stamp: str) -> None:
        """Recompute the accumulator from a full Tier-1 screen. Groups each prompt
        with the actions that follow it, tallies per-turn tokens, and attaches the
        redacted proof for every flagged risk."""
        ctx = next((e.args for e in result.events if e.kind == "session_start"), {}) or {}
        out = int(ctx.get("agent_output_tokens", 0) or 0)
        inp = int(ctx.get("agent_input_tokens", 0) or 0)
        self.global_tokens = {"input": inp, "output": out, "total": inp + out}

        by_seq: dict[int, list] = {}
        for f in result.findings:
            by_seq.setdefault(f.seq, []).append(f)

        intents: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for e in sorted(result.events, key=lambda ev: ev.seq):
            if e.action == "prompt" and (e.content or "").strip():
                cur = {
                    "seq": e.seq, "ts": e.ts,
                    "intent": _intent_preview(e.content),
                    "tokens": 0, "risks": [], "tools": [],
                }
                intents.append(cur)
            elif cur is not None:
                cur["tokens"] += int(e.tokens or 0)
                if e.target:
                    cur["tools"].append({"tool": e.tool or e.action, "target": e.target[:120]})
                for f in by_seq.get(e.seq, []):
                    cur["risks"].append({
                        "severity": f.severity, "category": f.category,
                        "proof": (f.evidence or {}).get("match", ""),
                    })
        self.intents = intents
        self.flagged = [t for t in intents if t["risks"]]
        self.scans += 1
        self.last_scan = stamp

    def save(self, *, state_dir: str | None = None) -> Path:
        p = self.path_for(self.session_key, state_dir=state_dir)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return p

    @property
    def flagged_count(self) -> int:
        return len(self.flagged)
