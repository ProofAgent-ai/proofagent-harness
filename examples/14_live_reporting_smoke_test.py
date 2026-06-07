"""End-to-end Live Reporting smoke test — no LLM keys required.

Runs every Live Reporting endpoint against the deployed backend with a
mocked payload (no real eval), then verifies the dashboard state matches.
Use this to prove the whole chain works WITHOUT spending real LLM tokens.

What it tests, in order:

  1.  POST  /api/v1/runs/start            → status 201, run_id, dashboard_url
  2.  POST  /api/v1/runs/{id}/turn-events → status 204 × N
  3.  POST  /api/v1/runs/{id}/events      → status 204 × M
  4.  POST  /api/v1/runs/{id}/sync        → status 200, run flips to completed
  5.  GET   /api/v1/runs/{id}             → status 200, has turns[], events[]
  6.  GET   /api/v1/runs/{id}/report      → status 200, has final_score

If ANY step fails, you'll see exactly which one + the real error message
(thanks to the protective wrappers + stage markers on the backend).

Run
---

    export PROOFAGENT_API_KEY="apk_live_..."
    python examples/14_live_reporting_smoke_test.py

    # Override backend if testing staging / self-hosted:
    PROOFAGENT_API_BASE=https://your-backend python examples/14_live_reporting_smoke_test.py
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)


# ─── Config ──────────────────────────────────────────────────────────

API_KEY = os.environ.get("PROOFAGENT_API_KEY", "")
BASE = os.environ.get(
    "PROOFAGENT_API_BASE",
    "https://apiproofagent-bmgnhxeeekf4awd2.centralus-01.azurewebsites.net",
).rstrip("/")
DASHBOARD = os.environ.get(
    "PROOFAGENT_DASHBOARD_BASE",
    "https://www.proofagent.ai",
).rstrip("/")

if not API_KEY:
    print("ERROR: PROOFAGENT_API_KEY not set", file=sys.stderr)
    print(
        "  Get one at https://www.proofagent.ai/dashboard/agents (+ New agent)",
        file=sys.stderr,
    )
    sys.exit(2)


HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "X-Harness-Version": "smoke-test",
}


# ─── Tiny test runner ────────────────────────────────────────────────


class Step:
    def __init__(self, name: str):
        self.name = name
        self.ok: bool | None = None
        self.detail = ""
        self.took_ms = 0
        self.t0 = 0.0

    def __enter__(self):
        print(f"  → {self.name}...", end=" ", flush=True)
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.took_ms = int((time.time() - self.t0) * 1000)
        if exc_type is None and self.ok is None:
            self.ok = True
        if self.ok:
            print(f"OK ({self.took_ms} ms)")
            if self.detail:
                for line in self.detail.split("\n"):
                    print(f"      {line}")
        else:
            print(f"FAIL ({self.took_ms} ms)")
            if self.detail:
                for line in self.detail.split("\n"):
                    print(f"      {line}")
            if exc:
                print(f"      exception: {type(exc).__name__}: {exc}")
        return True  # never propagate


def banner(title: str) -> None:
    bar = "═" * 64
    print()
    print(f"╔{bar}╗")
    print(f"║  {title:<62}║")
    print(f"╚{bar}╝")


def main() -> int:
    banner("ProofAgent Live Reporting smoke test")
    print(f"  Backend:    {BASE}")
    print(f"  Dashboard:  {DASHBOARD}")
    print(f"  API key:    {API_KEY[:18]}***")
    print()

    results: list[Step] = []
    run_id: str | None = None
    dashboard_url: str | None = None

    # ─── 1. POST /runs/start ─────────────────────────────────────
    with Step("POST /api/v1/runs/start") as s:
        results.append(s)
        body = {
            "agent_name": f"smoke-{uuid.uuid4().hex[:8]}",
            "agent_model": "smoke-bot",
            "harness_llm": "smoke-harness",
            "seed": 42,
            "turns_total": 2,
            "config": {"test": True},
        }
        r = httpx.post(f"{BASE}/api/v1/runs/start", json=body, headers=HEADERS, timeout=10)
        if r.status_code != 201:
            s.ok = False
            s.detail = f"HTTP {r.status_code}: {r.text[:600]}"
        else:
            data = r.json()
            run_id = data.get("run_id")
            dashboard_url = data.get("dashboard_url")
            s.detail = (
                f"run_id={run_id}\n"
                f"dashboard_url={dashboard_url}"
            )

    if not run_id:
        banner("ABORTED — /runs/start failed; nothing else can run")
        print("  See the FAIL detail above for the exact stage + error.")
        return 1

    # ─── 2. POST /turn-events × 2 ─────────────────────────────────
    for i in range(1, 3):
        with Step(f"POST /turn-events (turn {i})") as s:
            results.append(s)
            r = httpx.post(
                f"{BASE}/api/v1/runs/{run_id}/turn-events",
                json={
                    "turn_index": i,
                    "question": f"smoke question {i}",
                    "answer": f"smoke answer {i}",
                    "trap_name": "test_trap",
                    "outcome": "ok",
                    "duration_s": 0.5,
                    "defects": [],
                },
                headers=HEADERS, timeout=10,
            )
            if r.status_code != 204:
                s.ok = False
                s.detail = f"HTTP {r.status_code}: {r.text[:600]}"

    # ─── 3. POST /events (batch) ──────────────────────────────────
    with Step("POST /events (batch of 4)") as s:
        results.append(s)
        r = httpx.post(
            f"{BASE}/api/v1/runs/{run_id}/events",
            json={"events": [
                {"event_type": "plan_start", "detail": "smoke", "payload": {}, "turn": None},
                {"event_type": "turn_start", "detail": "smoke", "payload": {}, "turn": 1},
                {"event_type": "turn_end", "detail": "smoke", "payload": {}, "turn": 1},
                {"event_type": "report_end", "detail": "smoke", "payload": {}, "turn": None},
            ]},
            headers=HEADERS, timeout=10,
        )
        if r.status_code != 204:
            s.ok = False
            s.detail = f"HTTP {r.status_code}: {r.text[:600]}"

    # ─── 4. POST /sync ─────────────────────────────────────────────
    with Step("POST /sync (finalize)") as s:
        results.append(s)
        r = httpx.post(
            f"{BASE}/api/v1/runs/{run_id}/sync",
            json={
                "started_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": 1.0,
                "seed": 42,
                "harness_llm": "smoke-harness",
                "agent_model": "smoke-bot",
                "agent_name": "smoke-bot",
                "final_score": 7.5,
                "certification": "PASS",
                "per_metric": {
                    "task_success": 7.5,
                    "hallucination_resistance": 8.0,
                    "instruction_following": 7.0,
                    "safety": 9.0,
                    "manipulation_resistance": 6.5,
                },
                "config": {},
                "findings": [],
                "turns": [],
            },
            headers=HEADERS, timeout=15,
        )
        if r.status_code != 200:
            s.ok = False
            s.detail = f"HTTP {r.status_code}: {r.text[:600]}"

    # ─── 5. GET /runs/{id} ────────────────────────────────────────
    with Step("GET /runs/{id} (verify completed + has data)") as s:
        results.append(s)
        r = httpx.get(f"{BASE}/api/v1/runs/{run_id}", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            s.ok = False
            s.detail = f"HTTP {r.status_code}: {r.text[:600]}"
        else:
            data = r.json().get("data", {})
            status = data.get("status")
            turns_count = len(data.get("turns") or [])
            events_count = len(data.get("events") or [])
            tc = data.get("turns_completed")
            s.detail = (
                f"status={status}, turns_completed={tc}, "
                f"turns={turns_count}, events={events_count}"
            )
            if status != "completed":
                s.ok = False
                s.detail += f"\n  EXPECTED status=completed, GOT {status}"
            elif turns_count == 0:
                s.ok = False
                s.detail += "\n  EXPECTED turns array populated, GOT empty"
            elif events_count == 0:
                s.ok = False
                s.detail += "\n  EXPECTED events array populated, GOT empty"

    # ─── 6. GET /report ────────────────────────────────────────────
    with Step("GET /report (verify final score)") as s:
        results.append(s)
        r = httpx.get(f"{BASE}/api/v1/runs/{run_id}/report", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            s.ok = False
            s.detail = f"HTTP {r.status_code}: {r.text[:600]}"
        else:
            data = r.json().get("data", {})
            score = data.get("final_score")
            cert = data.get("certification_label")
            s.detail = f"final_score={score}, certification={cert}"
            if score is None:
                s.ok = False
                s.detail += "\n  EXPECTED final_score populated, GOT null"

    # ─── Summary ───────────────────────────────────────────────────
    ok_count = sum(1 for s in results if s.ok)
    fail_count = sum(1 for s in results if s.ok is False)
    banner(f"Smoke test: {ok_count} passed, {fail_count} failed")
    if fail_count > 0:
        print("  FAILED steps:")
        for s in results:
            if s.ok is False:
                print(f"    ✗ {s.name}")
                for line in (s.detail or "").split("\n"):
                    print(f"        {line}")
        print()
        print("  Diagnose:")
        print("    The stage marker in the FAIL detail tells you exactly which")
        print("    backend step failed (resolve_project_context / plan_turn_cap /")
        print("    quota_check / runs_insert / etc.).")
        return 1

    print(f"  All {ok_count} endpoints behaved correctly.")
    if dashboard_url:
        print()
        print("  Open the test run on the dashboard:")
        print(f"    {dashboard_url}")
    print()
    print("  ✓ Backend is healthy. SDK Live Reporting should work end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
