"""Rich-based progress reporter — listens to events and renders live progress."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from proofagent_harness.schemas import Event


class ProgressReporter:
    """Subscribe to Events and render a live status block."""

    def __init__(self, console: Console | None = None, enabled: bool = True) -> None:
        self.console = console or Console()
        self.enabled = enabled
        self.lines: list[str] = []
        self._live: Live | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.enabled:
            return
        self._live = Live(self._render(), console=self.console, refresh_per_second=8)
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    # ── event handler ────────────────────────────────────────────────

    def on_event(self, event: Event) -> None:
        if not self.enabled:
            return
        line = _format_event(event)
        if line:
            self.lines.append(line)
            if self._live is not None:
                self._live.update(self._render())

    # ── render ───────────────────────────────────────────────────────

    def _render(self) -> Any:
        table = Table.grid()
        for line in self.lines[-12:]:
            table.add_row(Text(line))
        return table


def _format_event(event: Event) -> str:
    t = event.type
    if t == "plan_start":
        return "[plan] Planning evaluation..."
    if t == "plan_end":
        return f"[plan] Done — {event.detail}"
    if t == "turn_start":
        return f"[turn {event.turn}] Start — {event.detail}"
    if t == "turn_end":
        return f"[turn {event.turn}] Done — {event.detail}"
    if t == "jury_round_start":
        return f"[jury] {event.detail}"
    if t == "jury_round_end":
        return f"[jury] {event.detail}"
    if t == "juror_scored":
        return f"        - {event.detail} ({event.metric})"
    if t == "consensus_check":
        return f"[consensus] {event.detail}"
    if t == "context_truncated":
        return f"[warn] context-budget trim: {event.detail}"
    if t == "report_start":
        return "[report] Building report..."
    if t == "report_end":
        return f"[report] Done — {event.detail}"
    if t == "done":
        return "[done]"
    if t == "error":
        return f"[error] {event.detail}"
    return ""
