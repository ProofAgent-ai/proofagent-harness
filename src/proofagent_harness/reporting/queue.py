"""Local pending reports queue.

When the backend is unreachable, evaluation reports are persisted to
``~/.proofagent/pending_reports/<idempotency_key>.json`` and can be
flushed later with the CLI command ``proofagent reporting sync``.

Disk format
-----------
One JSON file per queued report::

    {
        "idempotency_key": "...",
        "harness_version": "0.5.0",
        "queued_at_unix": 1717635012.345,
        "last_error": "Connection refused",
        "payload": { ... full report payload ... }
    }

The cache directory is created with mode 0700 (owner only). The API key
is NEVER written to disk — only the payload and the idempotency key.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_cache_dir() -> Path:
    """The default queue location: ``~/.proofagent/pending_reports/``."""
    home = Path.home()
    d = home / ".proofagent" / "pending_reports"
    d.mkdir(parents=True, exist_ok=True)
    # Owner only: 0700
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


@dataclass
class QueuedReport:
    """One queued report waiting to be flushed."""
    idempotency_key: str
    harness_version: str
    queued_at_unix: float
    last_error: str | None
    payload: dict[str, Any]
    path: Path


def write(
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    harness_version: str,
    last_error: str | None,
    cache_dir: Path | None = None,
) -> Path:
    """Persist a report to the local queue. Returns the file path written."""
    cd = cache_dir or default_cache_dir()
    out = cd / f"{idempotency_key}.json"
    out.write_text(json.dumps({
        "idempotency_key": idempotency_key,
        "harness_version": harness_version,
        "queued_at_unix": time.time(),
        "last_error": last_error,
        "payload": payload,
    }, indent=2, default=str))
    with contextlib.suppress(OSError):
        os.chmod(out, 0o600)
    return out


def list_queued(cache_dir: Path | None = None) -> list[QueuedReport]:
    """Enumerate every queued report sorted oldest first."""
    cd = cache_dir or default_cache_dir()
    out: list[QueuedReport] = []
    for p in sorted(cd.glob("*.json"), key=lambda f: f.stat().st_mtime):
        try:
            blob = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(QueuedReport(
            idempotency_key=blob.get("idempotency_key", p.stem),
            harness_version=blob.get("harness_version", "?"),
            queued_at_unix=blob.get("queued_at_unix", 0.0),
            last_error=blob.get("last_error"),
            payload=blob.get("payload", {}),
            path=p,
        ))
    return out


def remove(idempotency_key: str, cache_dir: Path | None = None) -> bool:
    """Remove a queued report by idempotency key. Returns True if removed."""
    cd = cache_dir or default_cache_dir()
    p = cd / f"{idempotency_key}.json"
    if p.exists():
        p.unlink()
        return True
    return False
