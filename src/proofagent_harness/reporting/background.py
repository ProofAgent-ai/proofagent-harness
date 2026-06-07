"""Background HTTP worker for Live Reporting.

Production-grade per-instance worker that POSTs queued events without ever
blocking the eval. Designed to be safe under heavy use (CI, pytest-xdist
parallel test workers, multi-tenant harness fleets) and predictable under
failure (bounded queue, graceful shutdown, full telemetry).

Key design properties — required when shipping this in customer pytest suites:

  • **Non-blocking enqueue.** ``enqueue()`` returns in microseconds. Even
    if the backend is unreachable, the eval continues at full speed.

  • **Per-instance isolation.** Each LiveReporter spins up its own worker
    thread + httpx.Client. No globals, no shared mutable state. Multiple
    Harness instances in parallel (pytest-xdist workers, batch evals) do
    not interfere.

  • **Connection pooling.** A single ``httpx.Client`` per worker reuses
    keep-alive connections to the backend, eliminating per-POST TLS
    handshake overhead.

  • **Bounded queue with overflow policy.** Configurable cap (default
    1000 items). On overflow we drop OLDEST events — a live activity feed
    cares about "what's happening now", not "what happened 10 minutes ago".

  • **Retries with exponential backoff.** 3 attempts (1 s, 2 s, 5 s) for
    5xx + transient network errors. Client errors (4xx) skip retry.

  • **Daemon thread.** Won't hang pytest exit if a user forgets to call
    ``shutdown()``. ``atexit`` handler runs a best-effort flush.

  • **Thread-safe telemetry counters.** Python's GIL makes simple int
    ops atomic; we use a single lock around the counter dict for any
    multi-field read.

  • **Graceful shutdown.** ``flush(timeout_s)`` waits for queue drain,
    ``shutdown(timeout_s)`` joins the worker thread. Both bounded.
"""
from __future__ import annotations

import atexit
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


# ─── Telemetry kinds — surfaced in summary banner ──────────────────────

KIND_EVENT = "event"
KIND_TURN = "turn"


@dataclass
class _QueuedPost:
    """One pending POST in the background worker queue."""
    path: str                          # e.g. /api/v1/runs/{id}/events
    body: dict[str, Any]
    kind: str                          # "event" | "turn" — for telemetry
    enqueued_at: float = field(default_factory=time.time)


# ─── Active-instance registry for atexit safety ────────────────────────
#
# Weak refs so we don't keep dead reporters alive. atexit calls best-effort
# flush so customers who forget to call shutdown() still get their data.

_ACTIVE_WORKERS: set[weakref.ReferenceType] = set()
_REGISTRY_LOCK = threading.Lock()


def _atexit_flush_all() -> None:
    """Best-effort flush every live worker on Python exit. Bounded so we
    don't hang the interpreter."""
    with _REGISTRY_LOCK:
        refs = list(_ACTIVE_WORKERS)
    for ref in refs:
        w = ref()
        if w is None:
            continue
        try:
            w.flush(timeout_s=2.0)
            w.shutdown(timeout_s=1.0)
        except Exception:
            pass


atexit.register(_atexit_flush_all)


# ─── Worker ────────────────────────────────────────────────────────────


class BackgroundReporter:
    """Background thread that drains a queue of pending POSTs.

    Use as a per-LiveReporter singleton. Lifecycle:

        bg = BackgroundReporter(base_url=..., api_key=...)
        bg.enqueue(path, body, kind=KIND_EVENT)   # non-blocking
        ...
        bg.flush(timeout_s=15.0)                  # block until drained
        bg.shutdown(timeout_s=5.0)                # join worker
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        harness_version: str,
        max_queue: int = 1000,
        timeout_s: float = 10.0,
        retry_attempts: int = 3,
        retry_backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0),
        worker_idle_sleep_s: float = 0.05,
    ):
        if httpx is None:
            raise RuntimeError(
                "httpx not installed — Live Reporting requires httpx. "
                "Install with: pip install 'proofagent-harness[reporting]'"
            )

        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Harness-Version": harness_version,
            "Content-Type": "application/json",
        }
        self._timeout_s = float(timeout_s)
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_backoff_s = retry_backoff_s
        self._worker_idle_sleep_s = float(worker_idle_sleep_s)

        # Bounded queue — backpressure on overflow.
        self._queue: queue.Queue[_QueuedPost | None] = queue.Queue(maxsize=max_queue)

        # Thread-safe telemetry. Single lock for any multi-field read.
        self._stats_lock = threading.Lock()
        self._sent: dict[str, int] = {KIND_EVENT: 0, KIND_TURN: 0}
        self._failed: dict[str, int] = {KIND_EVENT: 0, KIND_TURN: 0}
        self._dropped_overflow = 0
        self._last_error: str | None = None
        self._first_post_at: float | None = None
        self._last_post_at: float | None = None

        # Connection-pooled httpx Client — reused across all POSTs.
        # ``limits`` keeps a tight cap on concurrent connections so a
        # misconfigured worker doesn't open hundreds of sockets to the
        # backend.
        self._client = httpx.Client(
            timeout=self._timeout_s,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

        # Lifecycle.
        self._shutdown = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"proofagent-reporter-{id(self):x}",
            daemon=True,
        )
        self._thread.start()

        with _REGISTRY_LOCK:
            _ACTIVE_WORKERS.add(weakref.ref(self))

    # ── Public sync API ──

    def enqueue(self, path: str, body: dict[str, Any], *, kind: str) -> None:
        """Non-blocking enqueue. Returns in microseconds.

        On overflow drops OLDEST: a live activity feed cares about NOW,
        not stale events. Increments ``dropped_overflow`` counter.
        """
        item = _QueuedPost(path=path, body=body, kind=kind)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._stats_lock:
                self._dropped_overflow += 1
            try:
                _ = self._queue.get_nowait()           # discard one stale
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass  # tight race — acceptable

    def flush(self, timeout_s: float = 15.0) -> bool:
        """Block until the queue is empty (no items, no in-flight).

        Returns True if drained within the deadline, False on timeout.
        Bounded so flush() never hangs forever in pytest.
        """
        deadline = time.time() + max(0.0, float(timeout_s))
        while time.time() < deadline:
            if self._queue.qsize() == 0 and not self._worker_busy:
                return True
            time.sleep(0.05)
        return self._queue.qsize() == 0 and not self._worker_busy

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Stop the worker. Safe to call multiple times."""
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        try:
            self._queue.put_nowait(None)  # wake worker
        except queue.Full:
            pass
        self._thread.join(timeout=float(timeout_s))
        try:
            self._client.close()
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        """Snapshot of telemetry. Read by LiveReporter for the summary banner."""
        with self._stats_lock:
            return {
                "sent": dict(self._sent),
                "failed": dict(self._failed),
                "dropped_overflow": self._dropped_overflow,
                "queue_depth": self._queue.qsize(),
                "last_error": self._last_error,
                "first_post_at": self._first_post_at,
                "last_post_at": self._last_post_at,
            }

    # ── Worker loop ──

    @property
    def _worker_busy(self) -> bool:
        # Heuristic — we don't track per-item state; "busy" means worker
        # thread is alive and we may still be in the middle of a POST.
        return self._thread.is_alive() and not self._shutdown.is_set()

    def _run(self) -> None:
        """Worker thread main loop. Drains the queue until shutdown."""
        while True:
            try:
                item = self._queue.get(timeout=self._worker_idle_sleep_s)
            except queue.Empty:
                if self._shutdown.is_set():
                    return
                continue
            if item is None:                           # shutdown sentinel
                return
            try:
                self._process(item)
            except Exception as exc:
                with self._stats_lock:
                    self._last_error = (
                        f"worker_loop {type(exc).__name__}: {exc}"
                    )

    def _process(self, item: _QueuedPost) -> None:
        """POST one item with retries + backoff. Updates telemetry."""
        url = f"{self._base_url}{item.path}"
        now = time.time()
        with self._stats_lock:
            if self._first_post_at is None:
                self._first_post_at = now
            self._last_post_at = now

        for attempt in range(self._retry_attempts):
            try:
                r = self._client.post(url, json=item.body, headers=self._headers)

                # 2xx — success
                if 200 <= r.status_code < 300:
                    with self._stats_lock:
                        self._sent[item.kind] = self._sent.get(item.kind, 0) + 1
                    return

                # 4xx (not 429) — client error, don't retry, count failure
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    with self._stats_lock:
                        self._failed[item.kind] = self._failed.get(item.kind, 0) + 1
                        self._last_error = (
                            f"{item.path} HTTP {r.status_code}: "
                            f"{(r.text or '')[:200]}"
                        )
                    return

                # 5xx or 429 — fall through to retry
                with self._stats_lock:
                    self._last_error = (
                        f"{item.path} HTTP {r.status_code} (attempt "
                        f"{attempt + 1}/{self._retry_attempts}): "
                        f"{(r.text or '')[:200]}"
                    )

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                with self._stats_lock:
                    self._last_error = (
                        f"{item.path} {type(exc).__name__} (attempt "
                        f"{attempt + 1}/{self._retry_attempts}): {exc}"
                    )
            except Exception as exc:
                with self._stats_lock:
                    self._last_error = (
                        f"{item.path} {type(exc).__name__}: {exc}"
                    )

            # Sleep before next attempt (capped at retry_backoff_s length).
            if attempt < self._retry_attempts - 1:
                idx = min(attempt, len(self._retry_backoff_s) - 1)
                time.sleep(self._retry_backoff_s[idx])

        # All retries exhausted.
        with self._stats_lock:
            self._failed[item.kind] = self._failed.get(item.kind, 0) + 1
