"""Live progress UI for the harness."""

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

STAGE_WEIGHTS = {
    "setup": 1,
    "calibrate": 1,
    "plan": 1,
    "conduct_per_turn": 2,
    "jury_round_1": 3,
    "consensus_check": 1,
    "jury_round_2": 2,
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

    def start(self, turn_count: int = 8, *, calibrate: bool = True) -> None:
        """`calibrate=False` for modes that skip the pre-graph phase — its weight must
        stay out of the total, or the bar can never reach 100%."""
        if not self.enabled:
            return

        self._turn_count = max(1, turn_count)
        self._total = (
            STAGE_WEIGHTS["setup"]
            + (STAGE_WEIGHTS["calibrate"] if calibrate else 0)
            + STAGE_WEIGHTS["plan"]
            + STAGE_WEIGHTS["conduct_per_turn"] * self._turn_count
            + STAGE_WEIGHTS["jury_round_1"]
            + STAGE_WEIGHTS["consensus_check"]
            + STAGE_WEIGHTS["report"]
        )
        self._completed = 0.0
        self._turns_done = 0

        self._progress = Progress(
            SpinnerColumn(),
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
            if self._task_id is not None and self._completed < self._total:
                self._progress.update(self._task_id, completed=self._total)
            self._progress.stop()
            self._progress = None

    def on_event(self, event: Event) -> None:
        if not self.enabled or self._progress is None or self._task_id is None:
            return

        # Lines the user must actually READ, not watch flash past. The progress bar's
        # description is overwritten by the next event, so anything that reports a
        # DECISION about the run gets printed above the bar instead.
        if event.type in ("plan_turns", "context_assessed") and event.detail:
            with contextlib.suppress(Exception):
                self._progress.console.print(
                    f"[bold cyan]\\[{'plan' if event.type == 'plan_turns' else 'context'}]"
                    f"[/bold cyan] {escape(str(event.detail))}",
                    highlight=False,
                )

        if event.type == "error" and event.detail:
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

        self._progress.update(
            self._task_id,
            description=escape(description),
            completed=self._completed,
            stage=escape(stage),
        )

    def _stage_for(self, event: Event) -> tuple[str | None, str, float]:
        t = event.type

        if t == "setup_start":
            return "[setup]", "Checking Harness LLM reachability...", 0
        if t == "setup_done":
            return "[setup]", str(event.detail or "Harness LLM reachable"), STAGE_WEIGHTS["setup"]

        if t == "calibrate_start":
            return "[calibrate]", "Calibrating the evaluation...", 0
        if t == "calibrate_end":
            return "[calibrate]", str(event.detail or "Calibrated"), STAGE_WEIGHTS["calibrate"]

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
            advance = STAGE_WEIGHTS["jury_round_1"]
            return "[jury]", str(detail), advance

        if t == "juror_scored":
            return "[jury]", f"{event.detail} ({event.metric})", 0

        if t == "consensus_check":
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
        if t == "warning":
            # Degradation the run recovered from. Shown, because a silent fallback to
            # the noisier scoring path is exactly what a user needs to know about when
            # comparing two runs.
            return "[warn]", str(event.detail or "warning"), 0
        if t == "compliance_assessed":
            return "[compliance]", str(event.detail or "compliance assessed"), 0
        if t == "plan_turns":
            return "[plan]", str(event.detail or "turn budget"), 0
        if t == "context_assessed":
            return "[context]", str(event.detail or "context assessed"), 0

        if t == "done":
            return "[done]", "Evaluation complete", 0
        if t == "error":
            return "[error]", str(event.detail or "error"), 0

        return None, "", 0
