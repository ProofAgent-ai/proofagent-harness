"""Verify each Gemma evaluation end to end: PER valid, upload matches, no raw PII, proofs real.

usage: python verify_runs.py gemma_results
Prints one row per agent plus the failures whose proof reads like a refusal (to review by hand).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
GOV = Path(__file__).resolve().parents[3] / "proofagent-governance"
REFUSAL = re.compile(r"\b(i can(?:'|’)t|i cannot|i won(?:'|’)t|i'm unable|i am unable|not able to|must decline|refuse)\b", re.I)
RAW_PII = [r"\b\d{3}-\d{2}-\d{4}\b", r"\b(?:\d[ -]?){15,16}\b", r"[A-Za-z0-9._%+-]+@example\.(?:org|com)"]


def db(sql: str) -> str:
    out = subprocess.run([DOCKER, "compose", "exec", "-T", "db", "psql", "-U", "postgres", "-d", "proofagent", "-tAc", sql],
                         cwd=GOV, capture_output=True, text=True)
    return out.stdout.strip()


def check(run: Path) -> dict:
    log = (run / "run.log").read_text(errors="ignore")
    row = {"agent": run.name}
    per_path = run / "e2e.per.json"
    if not per_path.exists():
        row["status"] = "NO PER: " + (re.findall(r"(Not uploaded[^\n]*|aborted[^\n]*|PER export skipped[^\n]*)", log) or ["?"])[0][:80]
        return row
    v = subprocess.run([str(Path(__file__).resolve().parents[2] / ".venv/bin/eio-agents"), "validate", str(per_path)], capture_output=True, text=True)
    row["valid"] = v.stdout.strip().splitlines()[-1] if v.stdout.strip() else v.stderr.strip()[-60:]
    per = json.loads(per_path.read_text())
    rep = json.loads((run / "e2e.json").read_text())
    sc = per["scores"]
    row["decision"] = per["release_recommendation"]["state"]
    row["readiness"] = sc["readiness"]["value"]
    fails = [c for c in per["claims"] if c["state"] == "APPLICABLE_FAIL"]
    row["failures"] = len(fails)
    row["problems"] = len(per["findings"])
    # upload: the platform's copy is the same record
    m = re.search(r"record ([0-9a-f]{8})", log)
    if m:
        got = db(f"select decision, record->'scores'->'readiness'->>'value', jsonb_array_length(record->'claims') "
                 f"from per_records where id::text like '{m.group(1)}%' order by created_at desc limit 1")
        parts = got.split("|") if got else []
        same = len(parts) == 3 and parts[0].upper() == row["decision"] and int(parts[2]) == len(per["claims"])
        row["upload"] = "match" if same else f"MISMATCH {got}"
        sc_rows = db(f"select scenarios::text from per_record_scenarios where record_id::text like '{m.group(1)}%' limit 1")
        blob = json.dumps(per) + sc_rows
        leaks = [p for p in RAW_PII if re.search(p, blob)]
        row["raw_pii"] = "none" if not leaks else f"FOUND {leaks}"
    else:
        row["upload"] = "NOT UPLOADED"
    # proofs: every cited agent excerpt must be in that turn's real reply
    answers = {t["turn_index"]: t.get("answer") or "" for t in rep["transcript"]}
    refs = {r["id"]: r for r in per["evidence"]["refs"]}
    bad_proof, refusals = 0, []
    for c in fails:
        for rid in c.get("evidence") or []:
            r = refs.get(rid, {})
            if r.get("source_type") != "AGENT_ANSWER" or not r.get("excerpt"):
                continue
            ex = re.sub(r"\[REDACTED:[a-z_]+\]", "", r["excerpt"]).strip()
            ans = answers.get(r.get("turn_index"), "")
            if ex and ex[:40] not in ans and not any(ex[:25] in ans for _ in [0]):
                bad_proof += 1
            if REFUSAL.search(r["excerpt"][:200]) and not c["predicate"].endswith("clearly-refused"):
                refusals.append((r.get("turn_index"), c["predicate"].split(".")[-1], r["excerpt"][:140].replace("\n", " ")))
    row["proofs_not_in_reply"] = bad_proof
    row["review"] = refusals
    return row


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "gemma_results")
    rows = [check(r) for r in sorted(root.iterdir()) if r.is_dir()]
    for r in rows:
        print(json.dumps({k: v for k, v in r.items() if k != "review"}))
        for t, p, ex in r.get("review", []):
            print(f"    REVIEW turn {t} {p}: {ex}")
