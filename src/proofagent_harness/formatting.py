"""Display conventions, with no dependencies.

`pct` lives here rather than in `tools/report_tools` because the audit layer needs it while merely
READING a report, and `report_tools` imports `rich` for console rendering — so a formatting helper
dragged a terminal UI library into every consumer, including a web backend deriving a record from an
uploaded archive. One definition, importable from anywhere; `report_tools` re-exports it so no call
site changes.
"""

from __future__ import annotations


def pct(score_out_of_10: float | None) -> str:
    """Render a 0-10 score as a percentage — the ONE display convention.

    Every user-facing number reads out of 100 (final score, per-metric, context engineering,
    compliance, PAI), so nothing has to be mentally rescaled and a metric can be compared to an axis
    at a glance. Display only: the stored values stay on their native 0-10 scale, which is the
    report/upload contract.
    """
    if score_out_of_10 is None:
        return "—"
    return f"{round(float(score_out_of_10) * 10)}%"
