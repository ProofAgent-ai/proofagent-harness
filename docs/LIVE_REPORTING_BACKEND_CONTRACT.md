# Live Reporting — backend persistence contract (fix spec)

**Status:** the SDK + dashboard are correct; the backend is dropping data on
write. This document is the implementation-ready fix for whoever owns the
backend (the FastAPI service at
`apiproofagent-bmgnhxeeekf4awd2.centralus-01.azurewebsites.net`).

---

## Symptoms (observed in production)

On a **completed** multi-turn run, the dashboard shows:

| Field | Shows | Should show |
|---|---|---|
| Run score / certification / per-metric | ✅ correct | ✅ |
| Run-context: role, consensus, personas, seed | ✅ correct | ✅ |
| **Findings tab** | ❌ empty | 6 findings |
| **Jury audit tab** | ❌ empty | per-metric jury debate |
| **Tokens used** (Run context) | ❌ `—` | 748,714 |
| Live progress bar / turns_completed | ❌ stuck at 0 mid-run | 15/15 |

Meanwhile the **terminal** prints the full report (score, 6 metrics, 6
findings, 748,714 tokens) and every POST returns `2xx` (`sent=N failed=0`).

## Root cause

The completion `POST /api/v1/runs/{id}/sync` payload carries the full report —
`findings`, `tokens_used`, and a rich `config` blob (consensus_log,
technical_issues, token_breakdown, …) — but the handler **only persists the
flat top-level fields** (`final_score`, `certification`, `per_metric`). It does
**not**:

1. merge the `config` object into `runs.agent_config`, and
2. persist top-level `findings` + `tokens_used`, and
3. (live) bump `runs.turns_completed` / `runs.tokens_used` from `/turn-events`.

So `GET /runs/{id}` returns a row missing exactly those fields, and the
dashboard renders them empty. (The dashboard now derives progress/tokens/
transcript from the event stream as a stopgap, but findings + jury reasoning
are only in `/sync` and can't be reconstructed client-side.)

---

## The three endpoints + exact payloads the SDK sends

### 1. `POST /api/v1/runs/{run_id}/sync`  — completion (the main fix)

Body (`reporter._build_completion_payload`):

```jsonc
{
  "started_at": "2026-06-15T11:58:10Z",
  "duration_seconds": 229.0,
  "seed": 42,
  "harness_llm": "gpt-4.1-mini",
  "agent_model": "gpt-4.1-mini",
  "agent_name": "customer_support_agent",
  "final_score": 6.70,
  "certification": "NOT_READY",
  "per_metric": { "task_success": 7.5, "tool_use": 8.36, ... },
  "findings": [ { "severity": "fail", "metric": "...", "headline": "...",
                 "detail": "...(with Proof — citations + [Zero-tolerance])",
                 "recommendation": "..." }, ... ],          // ← TOP LEVEL
  "turns":  [ { "turn_index": 1, "question": "...", "answer": "...",
                "trap_name": "...", "outcome": "...", "duration_s": 1.8 }, ... ],
  "events": [ { "event_type": "turn_end", "detail": "...",
                "payload": { ... , "tokens_used": 149200 }, "turn": 1 }, ... ],
  "config": {                                                // ← RICH BLOB
    "mode": "multi_turn",
    "consensus_log":            { "tool_use": { "score": 3.0, "round_one": [...], "round_two": [...], ... }, ... },
    "confidence_per_metric":    { "tool_use": 0.33, ... },
    "severity_per_metric":      { "tool_use": "critical", ... },
    "technical_issues":         [ { "severity": "warn", "headline": "...", "detail": "..." }, ... ],
    "warnings":                 [ "Harness LLM refused on 2 turns", ... ],
    "executive_summary":        "The agent scored 6.7/10 ...",
    "production_ready":         "blocked",
    "top_risk":                 "...",
    "summary_text":             "...",
    "tokens_used":              748714,                      // ← grand total (incl jury)
    "token_breakdown":          { "total": 748714, "primary_prompt_tokens": ...,
                                  "primary_completion_tokens": ..., "primary_call_count": ...,
                                  "fallback_rate": 0.0, "token_split": {...} },
    "role": "...", "business_case": "...", "goal": "...",
    "consensus_strategy": "delphi", "metrics_active": [...], "traps_used": [...],
    "rubric_packs_applied": [...], "assertion_results": [...],
    "bundle_consistency_findings": [...], "per_artifact_scores": {...},
    "harness_metadata": { "personas": ["rigorous","lenient","contrarian"], "sdk_version": "..." }
  }
}
```

**Required handler behavior** (Python / SQLAlchemy-style pseudocode):

```python
@router.post("/api/v1/runs/{run_id}/sync")
async def sync_run(run_id: str, body: RunSyncRequest, db: Session):
    cfg = body.config or {}

    # 1) MERGE the rich config blob into runs.agent_config (JSONB ||).
    #    This is the line that's missing today. Use the DB-side merge so we
    #    don't clobber announce-time keys (role, consensus_strategy, seed …).
    # 2) Persist the top-level report fields, including findings + tokens.
    db.execute(text("""
        UPDATE runs
        SET agent_config   = COALESCE(agent_config, '{}'::jsonb) || CAST(:cfg AS jsonb),
            findings       = CAST(:findings AS jsonb),
            final_score    = :final_score,
            certification  = :certification,
            per_metric     = CAST(:per_metric AS jsonb),
            tokens_used    = :tokens_used,
            turns_completed= GREATEST(COALESCE(turns_completed,0), :turns_done),
            status         = 'completed',
            scored_at      = COALESCE(scored_at, now()),
            updated_at     = now()
        WHERE id = :run_id
    """), {
        "run_id": str(run_id),
        "cfg": json.dumps(cfg),
        "findings": json.dumps(body.findings or []),
        "final_score": body.final_score,
        "certification": body.certification,
        "per_metric": json.dumps(body.per_metric or {}),
        # grand total prefers config.token_breakdown.total, then config.tokens_used
        "tokens_used": int((cfg.get("token_breakdown") or {}).get("total")
                           or cfg.get("tokens_used") or 0),
        "turns_done": len(body.turns or []),
    })

    # 3) Backstop-backfill the turns + run_events tables IF they're empty
    #    (live POSTs may have been lost). Only when empty → no duplicates.
    _backfill_turns_if_empty(db, run_id, body.turns or [])
    _backfill_events_if_empty(db, run_id, body.events or [])
    db.commit()
    return {"status": "ok"}
```

> **Critical:** `RunSyncRequest` must NOT silently drop unknown fields. If it's
> a strict Pydantic model, add `findings`, `turns`, `events`, and `config: dict`
> to the schema (or accept `extra = "allow"`). A model that omits `config`
> is the most likely cause of the drop.

Required `runs` columns (add via migration if missing):
`agent_config jsonb`, `findings jsonb`, `per_metric jsonb`,
`tokens_used bigint`, `turns_completed int`, `final_score float`,
`certification text`, `status text`, `scored_at timestamptz`.

---

### 2. `POST /api/v1/runs/{run_id}/turn-events`  — per-turn (live progress)

Body (`reporter.append_turn`):
```jsonc
{ "turn_index": 4, "question": "...", "answer": "...", "trap_name": "...",
  "defects": [...], "outcome": "ok", "duration_s": 1.8,
  "total_turns": 15, "tokens_used": 40600 }
```

Handler must:
```python
# upsert the turn row
UPSERT INTO turns (id, run_id, turn_index, question, answer, trap_name,
                   outcome, defects, duration_s, ...) VALUES (...)
  ON CONFLICT (run_id, turn_index) DO UPDATE SET ...;

# AND bump the run's live counters (this is what drives the progress bar)
UPDATE runs
SET turns_completed = GREATEST(COALESCE(turns_completed,0), :turn_index),
    tokens_used     = GREATEST(COALESCE(tokens_used,0), :tokens_used),
    total_turns     = COALESCE(total_turns, :total_turns),
    updated_at      = now()
WHERE id = :run_id;
```

### 3. `POST /api/v1/runs/{run_id}/events`  — activity feed

Body (`reporter.append_event`): `{ event_type, detail, payload, turn }`.
Insert into `run_events (id, run_id, event_type, detail, payload jsonb, turn, created_at)`.
(This one already works — that's why "Avg response" + live progress render.)

---

### 4. `GET /api/v1/runs/{run_id}`  — what the dashboard reads

Must return, at minimum:
```jsonc
{
  "id", "status", "final_score", "certification", "per_metric",
  "tokens_used", "turns_completed", "total_turns",
  "findings":      [...],            // top-level
  "agent_config":  { ...full merged blob: consensus_log, technical_issues,
                     token_breakdown, warnings, executive_summary, ... },
  "turns":         [...],            // from the turns table
  "events":        [...]             // recent run_events
}
```
Do **not** project away `agent_config` or large JSONB fields for "performance" —
the dashboard needs the whole blob. If size is a concern, gate `turns`/`events`
behind `?include=turns,events` but keep `agent_config` + `findings` always.

---

## Verification

After deploying, run a live eval, then:

```bash
RUN=<run_id>
curl -s -H "Authorization: Bearer $PROOFAGENT_API_KEY" \
  "https://apiproofagent-bmgnhxeeekf4awd2.centralus-01.azurewebsites.net/api/v1/runs/$RUN" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);c=d.get('agent_config') or {};\
print('findings:',len(d.get('findings') or c.get('findings') or []));\
print('consensus_log keys:',len(c.get('consensus_log') or {}));\
print('technical_issues:',len(c.get('technical_issues') or []));\
print('tokens_used:',d.get('tokens_used'));\
print('turns_completed:',d.get('turns_completed'),'turns:',len(d.get('turns') or []))"
```

Expected AFTER the fix:
```
findings: 6
consensus_log keys: 6
technical_issues: 1
tokens_used: 748714
turns_completed: 15 turns: 15
```

## Acceptance checklist
- [ ] `RunSyncRequest` accepts `config`, `findings`, `turns`, `events` (no silent drop).
- [ ] `/sync` merges `config` into `runs.agent_config` via JSONB `||`.
- [ ] `/sync` persists top-level `findings` + `tokens_used` (grand total).
- [ ] `/turn-events` bumps `runs.turns_completed` + `tokens_used`.
- [ ] `GET /runs/{id}` returns `agent_config` (full), `findings`, `turns`, `events`.
- [ ] curl above shows non-zero findings / consensus / tokens.

Once this lands, the Findings tab, Jury audit tab, the Tokens "—", and the live
progress bar all populate from the server (the dashboard's event-stream
fallbacks become belt-and-suspenders rather than the only source).
