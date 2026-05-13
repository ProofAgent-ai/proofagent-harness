"""Live progress UI for the harness.

Renders a Rich progress bar showing the user where they are in the pipeline:

    [setup]      Checking Harness LLM reachability...                ░░░░░░░░░  0%
    [conduct]    Turn 4 / 8 - trap: pretexting / follow-up           ▓▓▓▓▓░░░ 56%
    [jury]       Round 1 (3 personas x 5 metrics)                    ▓▓▓▓▓▓░░ 78%
    [done]       Final score: 8.40 / 10  -  Certification: SILVER    ▓▓▓▓▓▓▓▓ 100%

Progress is event-driven — the harness emits Events through the on_event
hook, the bar updates accordingly. Verbose=False disables the UI entirely
(events still flow through any user-supplied callback).
"""

from __future__ import annotations

import contextlib

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from proofagent_harness.schemas import Event

# Stage budget — how the bar fills up as the eval moves forward.
# Each weight is a relative size. Conducting is the longest phase.
STAGE_WEIGHTS = {
    "setup": 1,
    "plan": 1,
    "conduct_per_turn": 2,    # multiplied by turn_count
    "jury_round_1": 3,
    "consensus_check": 1,
    "jury_round_2": 2,        # only fires when revote needed
    "report": 1,
}


class ProgressReporter:
    """Event-driven Rich progress bar."""

    def __init__(self, console: Console | None = None, enabled: bool = True) -> None:
        self.console = console or Console()
        self.enabled = enabled
        self._progress: Progress | None = None
        self._task_id: int | None = None
        self._completed: float = 0.0
        self._total: float = 100.0
        self._turn_count: int = 0
        self._turns_done: int = 0

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, turn_count: int = 8) -> None:
        if not self.enabled:
            return

        # Estimate total work units. Conducting dominates for large N.
        self._turn_count = max(1, turn_count)
        self._total = (
            STAGE_WEIGHTS["setup"]
            + STAGE_WEIGHTS["plan"]
            + STAGE_WEIGHTS["conduct_per_turn"] * self._turn_count
            + STAGE_WEIGHTS["jury_round_1"]
            + STAGE_WEIGHTS["consensus_check"]
            + STAGE_WEIGHTS["report"]
            # jury_round_2 added dynamically if/when consensus_check requests it
        )
        self._completed = 0.0
        self._turns_done = 0

        self._progress = Progress(
            SpinnerColumn(),
            # Style the column itself instead of wrapping with [bold]...[/bold]
            # markup — the literal "[setup]" / "[conduct]" / etc. would
            # otherwise be misparsed by Rich as a style tag and disappear.
            TextColumn("{task.fields[stage]:<11}", style="bold cyan"),
            TextColumn("{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        )
        self._progress.start()
        self._task_id = self._progress.add_task(
            description="Initializing...",
            total=self._total,
            stage="[init]",
        )

    def stop(self) -> None:
        if self._progress:
            # Make sure the bar shows 100% on a clean exit
            if self._task_id is not None and self._completed < self._total:
                self._progress.update(self._task_id, completed=self._total)
            self._progress.stop()
            self._progress = None

    # ── event handler ────────────────────────────────────────────────

    def on_event(self, event: Event) -> None:
        if not self.enabled or self._progress is None or self._task_id is None:
            return

        # Error events also get printed as a persistent red line ABOVE the
        # progress bar — otherwise they flash by in the bar's description
        # and the operator misses them in a 20-50 turn run.
        if event.type == "error" and event.detail:
            # never let UI rendering break the run
            with contextlib.suppress(Exception):
                self._progress.console.print(
                    f"[bold red]\\[error][/bold red] {escape(str(event.detail))}",
                    highlight=False,
                )

        stage, description, advance = self._stage_for(event)
        if stage is None:
            return

        if advance:
            self._completed = min(self._total, self._completed + advance)

        # Escape both fields — `[setup]`, `[conduct]`, etc. would otherwise
        # be eaten by Rich as malformed style tags. Same for any literal
        # brackets in the description (e.g. trap names that contain "[").
        self._progress.update(
            self._task_id,
            description=escape(description),
            completed=self._completed,
            stage=escape(stage),
        )

    # ── event → (stage, description, work_units_to_advance) ──────────

    def _stage_for(self, event: Event) -> tuple[str | None, str, float]:
        t = event.type

        if t == "setup_start":
            return "[setup]", "Checking Harness LLM reachability...", 0
        if t == "setup_done":
            return "[setup]", str(event.detail or "Harness LLM reachable"), STAGE_WEIGHTS["setup"]

        if t == "plan_start":
            return "[plan]", "Designing adversarial campaign...", 0
        if t == "plan_end":
            return "[plan]", str(event.detail or "Plan ready"), STAGE_WEIGHTS["plan"]

        if t == "turn_start":
            return (
                "[conduct]",
                f"Turn {event.turn} / {self._turn_count} - {event.detail}",
                0,
            )
        if t == "turn_end":
            self._turns_done += 1
            return (
                "[conduct]",
                f"Turn {event.turn} / {self._turn_count} done",
                STAGE_WEIGHTS["conduct_per_turn"],
            )

        if t == "jury_round_start":
            detail = event.detail or "scoring"
            advance = 0
            return "[jury]", str(detail), advance
        if t == "jury_round_end":
            detail = event.detail or "round complete"
            # Treat the FIRST round-end as completing jury_round_1 work units;
            # subsequent end events advance jury_round_2.
            advance = STAGE_WEIGHTS["jury_round_1"]
            return "[jury]", str(detail), advance

        if t == "juror_scored":
            # Per-juror events are too granular to advance the bar — show a
            # short status line in the description.
            return "[jury]", f"{event.detail} ({event.metric})", 0

        if t == "consensus_check":
            # If the consensus check announces a revote, expand the total
            # so the bar still reaches 100% at the end.
            metrics_to_revote = event.payload.get("metrics_to_revote") or []
            if metrics_to_revote:
                self._total += STAGE_WEIGHTS["jury_round_2"]
            return "[consensus]", str(event.detail or "computing consensus"), STAGE_WEIGHTS["consensus_check"]

        if t == "report_start":
            return "[report]", "Building scorecard...", 0
        if t == "report_end":
            return "[report]", str(event.detail or "Scorecard ready"), STAGE_WEIGHTS["report"]

        if t == "context_truncated":
            return "[warn]", f"context-budget trim: {event.detail}", 0

        if t == "done":
            return "[done]", "Evaluation complete", 0
        if t == "error":
            return "[error]", str(event.detail or "error"), 0

        return None, "", 0
