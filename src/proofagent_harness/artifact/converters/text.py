"""Plain-text + code converter — handles `.md`, `.txt`, `.py`, etc.

Tolerates broken bytes with `errors="replace"` so a single corrupt
character can't derail a whole evaluation.
"""

from __future__ import annotations

from pathlib import Path

from . import ArtifactConversionError


def read_plain_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to read {p}: {type(exc).__name__}: {exc}"
        ) from exc
