"""Session mode - govern a live AI coding-agent session (Claude Code, Cursor,
Copilot, Windsurf, …) as it works, not just at the release gate.

A *session* is a stream of normalized :class:`SessionEvent` s (tool calls, edits,
commands, diffs) captured from any coding tool. Tier-1 screening runs on every
event with **zero LLM tokens** (secrets, PII, dangerous commands, egress, scope,
dependencies, licenses); the result is turned into the same governance
``run-upload`` payload the harness already sends - with ``mode="session"`` - so a
session lands on the ProofAgent Governance dashboard alongside multi-turn and
artifact runs.

This package is fully **additive**: it imports nothing from the multi-turn /
artifact evaluation path and never mutates it. It reuses only the public
:mod:`proofagent_harness.governance` upload seam.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

from proofagent_harness.session.adapters import discover_session, load_session
from proofagent_harness.session.escalate import (
    Tier2Outcome,
    assess_critical_slice,
    build_critical_slice,
    should_escalate,
)
from proofagent_harness.session.events import SessionEvent, normalize_tool
from proofagent_harness.session.runner import (
    SessionResult,
    SessionRunner,
    build_session_payload,
)
from proofagent_harness.session.screen import SCREENS, ScreenFinding, screen_events

__all__ = [
    "SCREENS",
    "ScreenFinding",
    "SessionEvent",
    "SessionResult",
    "SessionRunner",
    "Tier2Outcome",
    "assess_critical_slice",
    "build_critical_slice",
    "build_session_payload",
    "discover_session",
    "load_session",
    "normalize_tool",
    "screen_events",
    "should_escalate",
]
