"""Re-test what broke the agent before, explore the rest: a per-agent ledger of failing attacks.

Each run of an agent in exploration mode (``--seed -1``) does three things:

1. **Re-test.** Attacks that failed in earlier runs are sent again WORD FOR WORD (the text before markers were
   planted; fresh markers are planted again so the code checks still work). Up to half the exam.
2. **Discover.** The rest of the exam is new attacks (no seed: new selection, new custom attacks, new wording).
3. **Account.** Every re-tested attack is marked ``fixed`` (no failing check this time) or ``still_open``; every
   failure on a new attack is ``new``. The ledger is updated and the result is recorded in the report.

The ledger is keyed by the agent's JOB (role, business case, goal), not its prompt or tools, so a new version of the
same agent is re-tested against the attacks the previous version failed: that is how a fix is confirmed.

Exploration is ``--seed -1`` (the CLI default seed 42 keeps the reproducible exam and skips all of this); ``PROOFAGENT_RETEST=0`` turns it off.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RETEST_SHARE = 0.5      # at most this share of the exam re-tests past failures; the rest discovers


def enabled(state: Any) -> bool:
    return state.get("seed") is None and os.environ.get("PROOFAGENT_RETEST", "1") != "0"


def agent_key(state: Any) -> str:
    basis = json.dumps({k: str(state.get(k) or "") for k in ("role", "business_case", "goal")}, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _path(key: str) -> Path:
    home = Path(os.environ.get("PROOFAGENT_HOME", str(Path.home() / ".proofagent")))
    return home / "ledger" / f"{key}.json"


def load(key: str) -> dict:
    try:
        return json.loads(_path(key).read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}


def save(key: str, ledger: dict) -> None:
    try:
        p = _path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ledger, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _item_id(trap_name: str, text: str) -> str:
    return hashlib.sha256(f"{trap_name}\n{text}".encode()).hexdigest()[:16]


def open_items(ledger: dict) -> list[dict]:
    """Open failures, the most recently failing first."""
    items = [dict(v, id=k) for k, v in (ledger.get("items") or {}).items() if v.get("status") != "fixed"]
    return sorted(items, key=lambda v: v.get("last_failed", ""), reverse=True)


def failing_turns(report: Any) -> dict[int, list[str]]:
    """turn_index -> the checks that failed on it (a failure of a negative check, or of a positive one)."""
    from proofagent_harness.checks import load_checks

    vocab = load_checks()
    out: dict[int, list[str]] = {}
    for v in getattr(report, "check_verdicts", None) or []:
        c = vocab.get(v.check_id)
        if c is None or v.observed is None:
            continue
        failed = (v.observed is True and c.polarity == "negative") or (v.observed is False and c.polarity == "positive")
        if failed:
            out.setdefault(int(v.turn_index), []).append(v.check_id)
    return out


def update(state: Any, report: Any) -> dict | None:
    """Update the agent's ledger from a finished run; return {retested, fixed, still_open, new} or None."""
    if not enabled(state):
        return None
    key = agent_key(state)
    ledger = load(key)
    items = ledger.setdefault("items", {})
    fails = failing_turns(report)
    traps = {getattr(t, "name", ""): t for t in (state.get("traps") or [])}
    custom = {t.name: t for t in (state.get("custom_traps") or [])}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary: dict[str, list[dict]] = {"retested": [], "fixed": [], "still_open": [], "new": []}
    for turn in getattr(report, "transcript", None) or []:
        if getattr(turn, "repeat_probe", None):
            continue          # probe repeats are not part of the exam
        idx = int(turn.turn_index)
        failed = fails.get(idx, [])
        if turn.retest_of:
            it = items.get(turn.retest_of)
            if it is None:
                continue
            row = {"turn": idx, "trap": turn.trap_name, "id": turn.retest_of}
            summary["retested"].append(row)
            it["times_retested"] = int(it.get("times_retested", 0)) + 1
            if failed:
                it.update(status="still_open", last_failed=now, last_checks=sorted(set(failed)),
                          times_failed=int(it.get("times_failed", 1)) + 1)
                summary["still_open"].append(row)
            else:
                it.update(status="fixed", fixed_at=now)
                summary["fixed"].append(row)
            continue
        if not failed or "control" in (turn.trap_name or ""):
            continue
        text = turn.attack_text or turn.question
        iid = _item_id(turn.trap_name, text)
        trap = traps.get(turn.trap_name) or custom.get(turn.trap_name)
        prev = items.get(iid)
        items[iid] = {
            "trap": turn.trap_name, "text": text, "checks": sorted(set(failed)),
            "trap_def": trap.model_dump(mode="json") if trap is not None and turn.trap_name not in traps else None,
            "status": "open", "first_failed": (prev or {}).get("first_failed", now), "last_failed": now,
            "times_failed": int((prev or {}).get("times_failed", 0)) + 1,
        }
        summary["new"].append({"turn": idx, "trap": turn.trap_name, "id": iid})
    save(key, ledger)
    return summary


def retest_specs(state: Any, n_turns: int) -> list[tuple[Any, dict]]:
    """(trap, ledger item) pairs to re-test this run, at most RETEST_SHARE of the exam."""
    from proofagent_harness.schemas import Trap

    if not enabled(state):
        return []
    ledger = load(agent_key(state))
    by_name = {t.name: t for t in (state.get("traps") or [])}
    cap = int(n_turns * RETEST_SHARE)
    out: list[tuple[Any, dict]] = []
    for it in open_items(ledger):
        if len(out) >= cap:
            break
        trap = by_name.get(it.get("trap", ""))
        if trap is None and it.get("trap_def"):
            try:
                trap = Trap(**it["trap_def"])
            except Exception:
                trap = None
        if trap is not None and it.get("text"):
            out.append((trap, it))
    return out
