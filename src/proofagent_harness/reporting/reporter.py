"""The Live Reporter — posts evaluation reports to the customer's tenant.

Design properties (in order of importance):

  1. **Safe by default.** Disabled unless explicitly enabled via
     ``live_reporting=True`` or by setting ``PROOFAGENT_API_KEY``.
     Without an API key, even ``live_reporting=True`` is a no op
     (with a single warning printed once per session).

  2. **Never blocks the evaluation.** All network failures are caught
     internally. On failure, the report is queued to local disk and
     the evaluation continues normally.

  3. **Idempotent.** Each report is keyed by a deterministic hash of
     (cell label, seed, agent model, harness LLM, started timestamp).
     Replays return the original ``dashboard_url``.

  4. **Privacy preserving.** The API key is never logged, never
     persisted to disk. Reports are sent over TLS to the customer
     tenant only.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

from proofagent_harness.reporting.background import (
    BackgroundReporter,
    KIND_EVENT,
    KIND_TURN,
)
from proofagent_harness.reporting.errors import (
    LiveReportingError,
    ReportingAuthError,
    ReportingQuotaError,
    ReportingUnavailableError,
)
from proofagent_harness.reporting.queue import default_cache_dir, write as queue_write

# The library version is the source of truth for the X-Harness-Version header
try:
    from proofagent_harness import __version__ as _HARNESS_VERSION
except ImportError:  # pragma: no cover
    _HARNESS_VERSION = "unknown"


DEFAULT_BASE_URL = "https://api.proofagent.ai"
DEFAULT_DASHBOARD_BASE = "https://www.proofagent.ai"


@dataclass
class ReportingConfig:
    """Configuration for the live reporter.

    All fields have sensible defaults from environment variables. Override
    explicitly only if you need a self hosted deployment or want to set
    project_id in code rather than via the API key.
    """
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    dashboard_base_url: str = DEFAULT_DASHBOARD_BASE
    cache_dir: Path = field(default_factory=default_cache_dir)
    enabled: bool = True
    timeout_seconds: float = 30.0
    retry_attempts: int = 3
    retry_delays_seconds: tuple[float, ...] = (1.0, 5.0, 30.0)
    print_progress: bool = True

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.environ.get("PROOFAGENT_API_KEY")
        # Allow PROOFAGENT_API_BASE / DASHBOARD_BASE overrides for self hosting
        self.base_url = os.environ.get("PROOFAGENT_API_BASE", self.base_url).rstrip("/")
        self.dashboard_base_url = os.environ.get(
            "PROOFAGENT_DASHBOARD_BASE", self.dashboard_base_url
        ).rstrip("/")

    @property
    def is_authorized(self) -> bool:
        """True if we have everything needed to attempt a network call."""
        return bool(self.enabled and self.api_key and httpx is not None)


class LiveReporter:
    """Reports completed evaluation cells to the ProofAgent dashboard.

    Production-grade design (we ship this to large customer pytest suites):

      • ``announce_run_start`` + ``report_completion`` are SYNCHRONOUS —
        they block until they get a response, because callers need the
        ``run_id`` from announce and the final ``dashboard_url`` from sync.

      • ``append_event`` + ``append_turn`` are NON-BLOCKING — they
        enqueue to a per-instance background worker that POSTs in a
        daemon thread with a pooled httpx.Client. Returns in microseconds
        so they never slow down the eval, even when called from inside
        an async LangGraph node mid-juror.

      • ``flush_pending`` waits (bounded) for the background worker to
        drain — called automatically by ``report_completion`` and at the
        end of ``Harness.aevaluate`` so the dashboard reflects everything
        that happened before the eval returns to the caller.

      • Tunable via env vars: ``PROOFAGENT_REPORTING_MAX_QUEUE`` (default
        1000), ``PROOFAGENT_REPORTING_TIMEOUT`` (default 10 s),
        ``PROOFAGENT_REPORTING_FLUSH_TIMEOUT`` (default 15 s).
    """

    def __init__(self, config: ReportingConfig | None = None) -> None:
        self.cfg = config or ReportingConfig()
        self._auth_disabled = False  # set True if we hit 401/403 once
        self._first_call = True
        # Telemetry mirrored from the background worker so the end-of-eval
        # banner has a single read path.
        self._announce_ok: bool | None = None
        self._announce_error: str | None = None
        self._announced_run_id: str | None = None
        self._announced_dashboard_url: str | None = None
        self._sync_ok: bool | None = None
        self._sync_error: str | None = None
        self._last_failure_detail: str | None = None
        # Background worker — lazy-started on first event/turn POST so we
        # don't spin a thread for callers who only do announce + sync.
        self._bg: BackgroundReporter | None = None

    # ── Background worker accessor ─────────────────────────────────────

    def _get_bg(self) -> BackgroundReporter | None:
        """Lazy-start the per-instance background worker. Safe to call repeatedly."""
        if self._bg is not None:
            return self._bg
        if not self._can_attempt():
            return None
        try:
            self._bg = BackgroundReporter(
                base_url=self.cfg.base_url,
                api_key=self.cfg.api_key or "",
                harness_version=_HARNESS_VERSION,
                max_queue=int(os.environ.get("PROOFAGENT_REPORTING_MAX_QUEUE", "1000")),
                timeout_s=float(os.environ.get("PROOFAGENT_REPORTING_TIMEOUT", "10.0")),
            )
        except Exception as exc:
            # Worker thread couldn't start — fall back to silent no-op.
            self._last_failure_detail = (
                f"background worker init failed: {type(exc).__name__}: {exc}"
            )
            self._bg = None
        return self._bg

    def flush_pending(self, timeout_s: float | None = None) -> bool:
        """Block until the background queue drains. Returns True if drained.

        Called automatically by report_completion + by the harness in its
        finally block, so customer code doesn't need to call it manually
        unless they want a checkpoint mid-eval.
        """
        if self._bg is None:
            return True
        if timeout_s is None:
            timeout_s = float(
                os.environ.get("PROOFAGENT_REPORTING_FLUSH_TIMEOUT", "15.0")
            )
        return self._bg.flush(timeout_s=timeout_s)

    def close(self) -> None:
        """Flush + shutdown the background worker. Idempotent.

        Production callers (pytest fixtures, long-lived service containers)
        should call this explicitly. The atexit handler in background.py
        also runs a best-effort flush on Python exit so forgetful callers
        don't lose data.
        """
        if self._bg is None:
            return
        try:
            self._bg.flush(timeout_s=15.0)
        finally:
            try:
                self._bg.shutdown(timeout_s=5.0)
            except Exception:
                pass
            self._bg = None

    def __enter__(self) -> "LiveReporter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- public

    def announce_run_start(
        self,
        *,
        cell_label: str,
        agent_name: str,
        agent_model: str,
        harness_llm: str,
        seed: int,
        turns_total: int,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Tell the backend a run is about to start so we can print a live URL.

        Returns the response dict ``{run_id, dashboard_url, stream_url}`` on
        success, or None if reporting is disabled / unauthorized / unreachable.

        Failures here are silent except for a one time auth warning. The
        evaluation pipeline continues regardless.
        """
        if not self._can_attempt():
            return None

        payload = {
            "agent_name": agent_name,
            "agent_model": agent_model,
            "harness_llm": harness_llm,
            "seed": seed,
            "turns_total": turns_total,
            "config": config or {},
        }
        # Loud-fail by default during the announce step so users can
        # self-diagnose env mistakes (wrong key type, wrong PROOFAGENT_API_BASE,
        # missing route on the deployed backend). We still NEVER raise — the
        # eval continues regardless. We just print one actionable line so the
        # user sees what went wrong instead of silently getting no URL.
        try:
            resp = self._request(
                "POST",
                "/api/v1/runs/start",
                json=payload,
                expected=(200, 201),
            )
        except ReportingAuthError as exc:
            self._announce_ok = False
            self._announce_error = f"auth: {exc}"
            if self.cfg.print_progress:
                self._print(f"[report]    Live URL skipped — auth rejected ({exc}).")
                self._print(f"            Key prefix used: {(self.cfg.api_key or '')[:18]}***")
                self._print(f"            Live Reporting keys start with 'live_eval_'. V1 'apk_live_*' keys")
                self._print(f"            cannot authenticate against /api/v1/runs/start. Register an agent")
                self._print(f"            on the dashboard ('+ New agent') to issue a live_eval_* key.")
            return None
        except ReportingUnavailableError as exc:
            self._announce_ok = False
            self._announce_error = f"unreachable: {exc}"
            if self.cfg.print_progress:
                self._print(f"[report]    Live URL skipped — backend unreachable ({exc}).")
                self._print(f"            Tried: POST {self.cfg.base_url}/api/v1/runs/start")
                self._print(f"            If you see 404, the backend at {self.cfg.base_url} doesn't have")
                self._print(f"            the V2 Live Reporting routes. Point PROOFAGENT_API_BASE at the")
                self._print(f"            backend that does (the V2 App Service URL, not the V1 one).")
            return None
        except LiveReportingError as exc:
            self._announce_ok = False
            self._announce_error = str(exc)
            if self.cfg.print_progress:
                self._print(f"[report]    Live URL skipped — {exc}")
            return None

        # Stash outcome on the reporter so summary() can render an
        # end-of-eval banner that the user CANNOT miss.
        self._announce_ok = True
        self._announced_run_id = resp.get("run_id") if isinstance(resp, dict) else None
        self._announced_dashboard_url = resp.get("dashboard_url") if isinstance(resp, dict) else None

        if self.cfg.print_progress:
            url = resp.get("dashboard_url") or f"{self.cfg.dashboard_base_url}/dashboard/agents"  # fallback — backend dashboard_url is the canonical URL
            self._print(f"[report]    Live run dashboard:")
            self._print(f"            {url}")
        return resp

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        detail: str | None = None,
        payload: dict[str, Any] | None = None,
        turn: int | None = None,
    ) -> None:
        """Stream a harness Event to the dashboard activity feed.

        NON-BLOCKING — enqueues to a per-instance background worker
        thread. Returns in microseconds even when the backend is slow or
        unreachable. The worker POSTs with a pooled httpx.Client +
        retries on 5xx/network errors with exponential backoff.

        Safe to call from inside an async LangGraph node mid-juror; the
        eval is never slowed down or blocked by reporting.
        """
        if not run_id or not event_type:
            return None
        bg = self._get_bg()
        if bg is None:
            if self.cfg.print_progress:
                self._print(f"[report] ⚠ bg worker UNAVAILABLE — event {event_type!r} DROPPED")
            return None
        bg.enqueue(
            path=f"/api/v1/runs/{run_id}/events",
            body={
                "events": [
                    {
                        "event_type": str(event_type)[:50],
                        "detail": (detail or "")[:1000],
                        "payload": payload or {},
                        "turn": turn,
                    }
                ]
            },
            kind=KIND_EVENT,
        )
        # VISIBLE diagnostic — print every event enqueue so the user can
        # see in real time that the SDK is firing events and the bg
        # worker is being fed. Without this, silent failures hide the
        # actual problem.
        if self.cfg.print_progress:
            stats = bg.stats()
            queue_depth = stats.get("queue_depth", 0)
            sent = stats.get("sent", {}).get(KIND_EVENT, 0)
            failed = stats.get("failed", {}).get(KIND_EVENT, 0)
            short_type = str(event_type)[:24]
            self._print(
                f"[report] ↗ event {short_type:<24} "
                f"queue={queue_depth} sent={sent} failed={failed}"
            )
        return None

    def append_turn(
        self,
        *,
        run_id: str,
        turn_index: int,
        question: str,
        answer: str,
        trap_name: str | None = None,
        defects: list[str] | None = None,
        outcome: str = "ok",
        duration_s: float = 0.0,
        total_turns: int | None = None,
    ) -> bool:
        """Commit a turn snapshot SYNCHRONOUSLY before the next turn starts.

        Per-turn is the unit the dashboard cares about most — the
        progress bar, the transcript tab, and the audit findings all key
        off it. We block on this POST (with retries) so the dashboard is
        guaranteed to reflect turn N before the harness moves to turn N+1.

        Events (juror_scored, plan_*, etc.) remain async via the bg
        worker — those are best-effort. Turns are durable.

        Flow:
          1. Flush the background events queue so any events fired
             DURING this turn land on the dashboard BEFORE the turn
             marker (preserves chronological order on the activity feed).
          2. Sync POST /turn-events with full structured payload.
          3. Retry up to 3 times on 5xx / network error (exponential).
          4. Print a hard-to-miss progress bar showing N/M synced.

        Returns True on success, False on failure. Failure is logged +
        counted in the summary banner; the eval still continues
        (the final /sync re-uploads the full transcript as a backstop).
        """
        if not run_id:
            return False

        # 1. Flush events queue so per-turn events land before the turn marker
        bg = self._bg
        if bg is not None:
            try:
                bg.flush(timeout_s=2.0)
            except Exception:
                pass

        # 2. Sync POST with bounded retries
        if not self._can_attempt():
            return False
        url = f"{self.cfg.base_url}/api/v1/runs/{run_id}/turn-events"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "X-Harness-Version": _HARNESS_VERSION,
            "Content-Type": "application/json",
        }
        body = {
            "turn_index": int(turn_index),
            "trap_name": trap_name,
            "question": (question or "")[:8000],
            "answer": (answer or "")[:16000],
            "outcome": outcome or "ok",
            "duration_s": float(duration_s or 0.0),
            "defects": list(defects or []),
        }

        backoffs = (0.5, 1.5, 3.0)
        last_err: str | None = None
        for attempt in range(len(backoffs)):
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=15.0)
                if 200 <= r.status_code < 300:
                    self._print_turn_progress(turn_index, total_turns, ok=True)
                    return True
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    last_err = f"HTTP {r.status_code}: {(r.text or '')[:160]}"
                    break  # client error — don't retry
                last_err = f"HTTP {r.status_code}: {(r.text or '')[:160]}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < len(backoffs) - 1:
                time.sleep(backoffs[attempt])

        self._print_turn_progress(turn_index, total_turns, ok=False, err=last_err)
        if not self._last_failure_detail:
            self._last_failure_detail = f"/turn-events: {last_err}"
        return False

    def _print_turn_progress(
        self,
        turn_index: int,
        total_turns: int | None,
        *,
        ok: bool,
        err: str | None = None,
    ) -> None:
        """Hard-to-miss per-turn progress bar in the terminal."""
        if not self.cfg.print_progress:
            return
        if total_turns and total_turns > 0:
            pct = int(100 * turn_index / total_turns)
            bar_width = 40
            filled = int(bar_width * turn_index / total_turns)
            bar = "█" * filled + "░" * (bar_width - filled)
            label = f"turn {turn_index}/{total_turns}"
            if ok:
                self._print(f"[report] {bar} {pct:3d}%  {label}  → synced")
            else:
                self._print(f"[report] {bar} {pct:3d}%  {label}  ✗ FAILED: {err}")
        else:
            mark = "→ synced" if ok else f"✗ FAILED: {err}"
            self._print(f"[report] turn {turn_index} {mark}")

    def report_completion(
        self,
        *,
        run_id: str | None,
        cell_label: str,
        report_blob: dict[str, Any],
        transcript: list[dict[str, Any]] | None = None,
        findings: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Send the final completion record for a cell.

        If ``run_id`` is None (announce_run_start failed or was skipped),
        we send a self contained sync via POST /api/v1/runs/sync that
        creates the run and stores the result atomically.

        On network failure after retries, the payload is queued locally.
        The evaluation never fails because of this method.
        """
        if not self._can_attempt():
            return None

        # Drain pending background POSTs FIRST so all per-turn events have
        # had a chance to land before the final transcript is written. This
        # guarantees the dashboard's last poll (before /sync flips status
        # to completed) sees the fullest possible live state.
        try:
            self.flush_pending()
        except Exception:
            pass

        payload = self._build_completion_payload(
            report_blob, transcript or [], findings or [], events or [],
        )
        idempotency_key = self._idempotency_key(cell_label, payload)

        path = (f"/api/v1/runs/{run_id}/sync" if run_id else "/api/v1/runs/sync")
        last_error: str | None = None

        for attempt, delay in enumerate(
            (self.cfg.retry_delays_seconds + (0.0,))[: self.cfg.retry_attempts]
        ):
            try:
                resp = self._request(
                    "POST",
                    path,
                    json=payload,
                    headers_extra={"X-Idempotency-Key": idempotency_key},
                    expected=(200, 201),
                )
                if self.cfg.print_progress:
                    url = resp.get("dashboard_url") or f"{self.cfg.dashboard_base_url}/dashboard/agents"  # fallback — backend dashboard_url is the canonical URL
                    self._print(f"[report] sync OK -> {url}")
                self._sync_ok = True
                return resp
            except ReportingAuthError as exc:
                self._auth_disabled = True
                self._sync_ok = False
                self._sync_error = f"auth: {exc}"
                self._print_once_auth_disabled(str(exc))
                # Queue locally so a key rotation can recover the data later
                queue_write(payload, idempotency_key=idempotency_key,
                            harness_version=_HARNESS_VERSION,
                            last_error=str(exc), cache_dir=self.cfg.cache_dir)
                return None
            except ReportingUnavailableError as exc:
                last_error = str(exc)
                if attempt < self.cfg.retry_attempts - 1 and delay > 0:
                    time.sleep(delay)
                continue
            except ReportingQuotaError as exc:
                last_error = str(exc)
                break
            except LiveReportingError as exc:
                last_error = str(exc)
                break

        # All retries exhausted — queue locally
        self._sync_ok = False
        self._sync_error = last_error or "all retries exhausted"
        cached_path = queue_write(
            payload,
            idempotency_key=idempotency_key,
            harness_version=_HARNESS_VERSION,
            last_error=last_error,
            cache_dir=self.cfg.cache_dir,
        )
        if self.cfg.print_progress:
            self._print(
                f"[report] backend unreachable -> queued at {cached_path}"
            )
            self._print(
                f"         flush later with: proofagent reporting sync"
            )
        return None

    # ------------------------------------------------------- end-of-eval summary

    def summary(self) -> dict[str, Any]:
        """Snapshot of every Live Reporting POST attempted this run.

        Pulls per-event/per-turn counters from the background worker so
        the numbers reflect what the WORKER actually did, not what the
        caller intended. announce + sync counters stay on the reporter
        because those are synchronous (no background queue).
        """
        bg_stats = self._bg.stats() if self._bg is not None else {
            "sent": {KIND_EVENT: 0, KIND_TURN: 0},
            "failed": {KIND_EVENT: 0, KIND_TURN: 0},
            "dropped_overflow": 0,
            "queue_depth": 0,
            "last_error": None,
        }
        # Turn POSTs are now SYNCHRONOUS (not through bg worker) — read
        # from the sync counter we update from harness on each turn_end.
        return {
            "announce_ok": self._announce_ok,
            "announce_error": self._announce_error,
            "run_id": self._announced_run_id,
            "dashboard_url": self._announced_dashboard_url,
            "turn_events_sent": getattr(self, "_turn_events_sent_sync", 0),
            "turn_events_failed": getattr(self, "_turn_events_failed_sync", 0),
            "events_sent": bg_stats["sent"].get(KIND_EVENT, 0),
            "events_failed": bg_stats["failed"].get(KIND_EVENT, 0),
            "dropped_overflow": bg_stats["dropped_overflow"],
            "queue_depth_remaining": bg_stats["queue_depth"],
            "sync_ok": self._sync_ok,
            "sync_error": self._sync_error,
            "first_failure_detail": self._last_failure_detail or bg_stats.get("last_error"),
        }

    def print_summary_banner(self) -> None:
        """Print a hard-to-miss boxed summary at end of eval. Called from
        ``Harness.aevaluate`` after the eval finishes. Always prints, even
        when everything succeeded — the user wants confirmation either way.
        """
        s = self.summary()
        bar = "═" * 64
        self._print("")
        self._print(f"╔{bar}╗")
        self._print(f"║  Live Reporting summary                                          ║")
        self._print(f"╠{bar}╣")

        def _line(label: str, value: str) -> None:
            text = f"  {label:<20} {value}"
            text = text[:64] + " " * max(0, 64 - len(text))
            self._print(f"║{text}║")

        ok_mark = "✓" if s["announce_ok"] else ("✗" if s["announce_ok"] is False else "—")
        _line("/runs/start:", f"{ok_mark} {s['announce_error'] or 'OK'}")
        if s["run_id"]:
            _line("run_id:", str(s["run_id"])[:32])
        _line("/turn-events:", f"{s['turn_events_sent']} sent / {s['turn_events_failed']} failed")
        _line("/events:", f"{s['events_sent']} sent / {s['events_failed']} failed")
        if s.get("dropped_overflow", 0) > 0:
            _line("queue dropped:", f"{s['dropped_overflow']} (backpressure — bump PROOFAGENT_REPORTING_MAX_QUEUE)")
        if s.get("queue_depth_remaining", 0) > 0:
            _line("queue at exit:", f"{s['queue_depth_remaining']} not yet flushed")
        sync_mark = "✓" if s["sync_ok"] else ("✗" if s["sync_ok"] is False else "—")
        _line("/sync:", f"{sync_mark} {s['sync_error'] or ('OK' if s['sync_ok'] else 'not called')}")
        if s["first_failure_detail"]:
            self._print(f"║  first failure detail:                                           ║")
            # Wrap long failure text into multiple boxed lines.
            detail = s["first_failure_detail"]
            while detail:
                chunk, detail = detail[:60], detail[60:]
                text = f"    {chunk}"
                text = text[:64] + " " * max(0, 64 - len(text))
                self._print(f"║{text}║")
        if s["dashboard_url"]:
            self._print(f"║                                                                  ║")
            _line("Dashboard:", "")
            url = s["dashboard_url"]
            while url:
                chunk, url = url[:60], url[60:]
                text = f"    {chunk}"
                text = text[:64] + " " * max(0, 64 - len(text))
                self._print(f"║{text}║")
        self._print(f"╚{bar}╝")
        self._print("")

    # ---------------------------------------------------------------- helpers

    def _can_attempt(self) -> bool:
        if not self.cfg.enabled or self._auth_disabled:
            return False
        if not self.cfg.api_key:
            if self._first_call:
                self._print_once_no_key()
            return False
        if httpx is None:
            if self._first_call:
                self._print(
                    "[report] httpx not installed — live reporting disabled.\n"
                    "         install with: pip install 'proofagent-harness[reporting]'"
                )
            self._first_call = False
            return False
        if self.cfg.base_url.startswith("http://"):
            # Refuse to send credentials over plaintext unless it's localhost
            host_ok = (
                "127.0.0.1" in self.cfg.base_url or "localhost" in self.cfg.base_url
            )
            if not host_ok:
                if self._first_call:
                    self._print(
                        "[report] refusing plaintext HTTP — live reporting disabled.\n"
                        "         set PROOFAGENT_API_BASE to an https:// URL"
                    )
                self._first_call = False
                return False
        return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
        headers_extra: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        """Single round trip. Raises typed errors. Caller handles retry."""
        url = f"{self.cfg.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "X-Harness-Version": _HARNESS_VERSION,
            "Content-Type": "application/json",
            **(headers_extra or {}),
        }
        self._first_call = False
        try:
            r = httpx.request(
                method, url, json=json, headers=headers,
                timeout=self.cfg.timeout_seconds,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ReportingUnavailableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LiveReportingError(str(exc)) from exc

        if r.status_code in expected:
            try:
                return r.json()
            except ValueError:
                return {}
        # Specific error categorization
        body = (r.text or "")[:240]
        if r.status_code in (401, 403):
            raise ReportingAuthError(f"{r.status_code}: {body}")
        if r.status_code == 429:
            raise ReportingQuotaError(f"429 quota: {body}")
        if r.status_code >= 500:
            raise ReportingUnavailableError(f"{r.status_code}: {body}")
        raise LiveReportingError(f"{r.status_code}: {body}")

    def _print(self, msg: str) -> None:
        try:
            print(msg, file=sys.stderr if not self.cfg.print_progress else sys.stdout, flush=True)
        except Exception:
            pass

    def _print_once_no_key(self) -> None:
        if not self._first_call:
            return
        self._first_call = False
        self._print(
            "[report] live_reporting=True but PROOFAGENT_API_KEY is not set.\n"
            "         reporting disabled for this session. set the env var to enable."
        )

    def _print_once_auth_disabled(self, detail: str) -> None:
        self._print(
            f"[report] API key rejected ({detail[:80]}); live reporting disabled."
        )

    @staticmethod
    def _idempotency_key(cell_label: str, payload: dict[str, Any]) -> str:
        h = hashlib.sha256()
        h.update(cell_label.encode("utf-8", errors="replace"))
        for k in ("seed", "agent_model", "harness_llm", "started_at"):
            v = payload.get(k, "")
            h.update(str(v).encode("utf-8", errors="replace"))
        return h.hexdigest()[:32]

    @staticmethod
    def _build_completion_payload(
        report: dict[str, Any],
        transcript: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Normalize the harness Report into the wire format the backend expects."""
        return {
            "started_at": report.get("started_at"),
            "duration_seconds": report.get("duration_seconds", 0),
            "seed": report.get("seed", 0),
            "harness_llm": report.get("harness_llm", ""),
            "agent_model": report.get("agent_model", ""),
            "agent_name": report.get("agent_name", ""),
            "final_score": report.get("final_score"),
            "certification": report.get("certification"),
            "per_metric": report.get("per_metric", {}),
            "config": report.get("config", {}),
            "findings": findings,
            "turns": transcript,
            # Full event timeline — backend backfills run_events table
            # from this if live POSTs were lost (only when the table is
            # empty — no duplicates with live events).
            "events": events or [],
        }
