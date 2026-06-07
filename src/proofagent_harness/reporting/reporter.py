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
    """Reports completed evaluation cells to the ProofAgent dashboard."""

    def __init__(self, config: ReportingConfig | None = None) -> None:
        self.cfg = config or ReportingConfig()
        self._auth_disabled = False  # set True if we hit 401/403 once
        self._first_call = True
        # Telemetry counters — surfaced at the end of every eval via
        # ``summary()`` so the user can SEE exactly how many per-turn POSTs
        # + event POSTs were attempted and how many succeeded. Critical
        # for diagnosing the "nothing reached the dashboard" failure mode:
        # without these you can't tell if the SDK swallowed silent errors.
        self._announce_ok: bool | None = None
        self._announce_error: str | None = None
        self._announced_run_id: str | None = None
        self._announced_dashboard_url: str | None = None
        self._turn_events_sent = 0
        self._turn_events_failed = 0
        self._events_sent = 0
        self._events_failed = 0
        self._sync_ok: bool | None = None
        self._sync_error: str | None = None
        self._last_failure_detail: str | None = None

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
        """Stream a single harness Event to the dashboard for the live
        activity feed.

        Fire-and-forget — catches every exception, 3 s timeout, never
        retries. Losing a single event is fine; the goal is the
        terminal-style log on the dashboard, not a durable audit trail
        (that's what /sync does at the end).

        Why per-event POSTs instead of batching: simpler model, the
        harness emits ~50 events per typical eval, network is cheap.
        Backend accepts batches too via the {events:[...]} shape so
        we can flush a queue here later if it becomes a bottleneck.
        """
        if not self._can_attempt() or not run_id or not event_type:
            return None
        path = f"/api/v1/runs/{run_id}/events"
        body = {
            "events": [
                {
                    "event_type": str(event_type)[:50],
                    "detail": (detail or "")[:1000],
                    "payload": payload or {},
                    "turn": turn,
                }
            ]
        }
        try:
            url = f"{self.cfg.base_url}{path}"
            headers = {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "X-Harness-Version": _HARNESS_VERSION,
                "Content-Type": "application/json",
            }
            r = httpx.post(url, json=body, headers=headers, timeout=3.0)
            if 200 <= r.status_code < 300:
                self._events_sent += 1
            else:
                self._events_failed += 1
                if not self._last_failure_detail:
                    self._last_failure_detail = (
                        f"/events HTTP {r.status_code}: {(r.text or '')[:200]}"
                    )
        except Exception as exc:
            self._events_failed += 1
            if not self._last_failure_detail:
                self._last_failure_detail = f"/events network: {type(exc).__name__}: {exc}"
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
    ) -> None:
        """Stream a per-turn update to the dashboard so the progress bar climbs
        and the transcript fills in live.

        Fire-and-forget: catches every exception. We deliberately do NOT
        retry, queue, or warn here — losing a single turn-event is fine
        (the final ``report_completion`` POST contains the full transcript),
        and we must NEVER slow the eval down or raise from the conductor loop.

        Times out aggressively (3 s) so even a flaky network can't bottleneck
        the turn-by-turn loop.
        """
        if not self._can_attempt() or not run_id:
            return None
        path = f"/api/v1/runs/{run_id}/turn-events"
        payload = {
            "turn_index": int(turn_index),
            "trap_name": trap_name,
            "question": (question or "")[:8000],   # belt-and-braces against
            "answer": (answer or "")[:16000],       # accidentally huge payloads
            "outcome": outcome or "ok",
            "duration_s": float(duration_s or 0.0),
            "defects": list(defects or []),
        }
        try:
            # Shorter timeout than the default — we don't want a slow network
            # to slow the eval. Backend returns 204 on success.
            url = f"{self.cfg.base_url}{path}"
            headers = {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "X-Harness-Version": _HARNESS_VERSION,
                "Content-Type": "application/json",
            }
            r = httpx.post(url, json=payload, headers=headers, timeout=3.0)
            if 200 <= r.status_code < 300:
                self._turn_events_sent += 1
            else:
                self._turn_events_failed += 1
                if not self._last_failure_detail:
                    self._last_failure_detail = (
                        f"/turn-events HTTP {r.status_code}: {(r.text or '')[:200]}"
                    )
        except Exception as exc:
            # Per-turn updates are best-effort. The final /sync POST is the
            # source of truth for the transcript anyway. Silent on failure
            # but COUNTED so summary() can show how many were lost.
            self._turn_events_failed += 1
            if not self._last_failure_detail:
                self._last_failure_detail = (
                    f"/turn-events network: {type(exc).__name__}: {exc}"
                )
        return None

    def report_completion(
        self,
        *,
        run_id: str | None,
        cell_label: str,
        report_blob: dict[str, Any],
        transcript: list[dict[str, Any]] | None = None,
        findings: list[dict[str, Any]] | None = None,
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

        payload = self._build_completion_payload(report_blob, transcript or [], findings or [])
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

        The harness prints this after the eval finishes (see
        ``Harness.aevaluate``) so the user sees a hard-to-miss banner with
        exact counts + the first failure detail. Diagnoses the silent-fail
        mode where the SDK swallowed errors and nothing reached the
        dashboard.
        """
        return {
            "announce_ok": self._announce_ok,
            "announce_error": self._announce_error,
            "run_id": self._announced_run_id,
            "dashboard_url": self._announced_dashboard_url,
            "turn_events_sent": self._turn_events_sent,
            "turn_events_failed": self._turn_events_failed,
            "events_sent": self._events_sent,
            "events_failed": self._events_failed,
            "sync_ok": self._sync_ok,
            "sync_error": self._sync_error,
            "first_failure_detail": self._last_failure_detail,
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
        }
