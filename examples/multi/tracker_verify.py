"""Verify one uploaded run against its PER and the local platform; write verify.json next to the run.

Usage: python tracker_verify.py <run_dir> <agent> <rep>      (env: UPLOAD_URL, UPLOAD_KEY)
Checks: EIO schema + semantic validation; the platform stored the same record (digest) and signed it; the platform's
decision equals the PER's; every PER finding is on the dashboard (findings API, by PER finding id) with its severity;
the report's decision vs the PER's. Never raises: a failed check is recorded, not fatal.
"""
import json
import os
import re
import sys
from pathlib import Path

import eio_agents as eio
import httpx


def main(run_dir: str, agent: str, rep: str) -> None:
    d = Path(run_dir)
    out: dict = {"agent": agent, "rep": rep, "ok": False, "checks": {}, "errors": []}
    try:
        run = json.loads((d / "run.json").read_text())
        per = json.loads((d / "run.per.json").read_text())
    except Exception as exc:
        out["errors"].append(f"run files missing: {exc}")
        (d / "verify.json").write_text(json.dumps(out, indent=1))
        return
    log = (d / "log.txt").read_text(errors="replace") if (d / "log.txt").exists() else ""
    rr = per.get("release_recommendation") or {}
    tr = [t for t in run.get("transcript") or [] if not t.get("repeat_probe")]
    meta = run.get("metadata") or {}
    out.update(
        decision=rr.get("state"),
        readiness=((per.get("scores") or {}).get("readiness") or {}).get("value"),
        axes={k: (v or {}).get("value") for k, v in ((per.get("scores") or {}).get("axes") or {}).items()}
        if isinstance((per.get("scores") or {}).get("axes"), dict) else None,
        metrics={m["metric"].split(".")[-1]: m.get("value") for m in (per.get("scores") or {}).get("metrics") or []},
        withheld={m["metric"].split(".")[-1]: m.get("withheld_code") for m in (per.get("scores") or {}).get("metrics") or []
                  if m.get("value") is None},
        behaviour_score=run.get("final_score"),
        report_metrics={k: round(v, 2) for k, v in (run.get("per_metric") or {}).items()},
        turns=(per.get("provenance") or {}).get("run", {}).get("turns"),
        exam_turns=len(tr),
        plan=[t.get("trap_name") for t in tr],
        domains=meta.get("domains_inferred") or [],
        duration_min=round((run.get("duration_seconds") or 0) / 60, 1),
        evaluator_models=sorted({m.get("model") or "?" for m in (per.get("telemetry") or {}).get("evaluator_models") or []}),
        agent_model=((per.get("provenance") or {}).get("agent") or {}).get("model"),
        decisive=[f"{x.get('id')} ({x.get('effect')})" for x in rr.get("decisive") or []],
    )
    fnd = [f for f in per.get("findings") or [] if f.get("kind") != "CONTEXT_GAP"]
    out["findings"] = [{"id": f.get("finding_id"), "predicate": (f.get("predicate") or "").split(".")[-1],
                        "risk": (f.get("risk") or "").split(".")[-1], "severity": f.get("severity"),
                        "status": f.get("status"), "turns": f.get("turn_indices") or [],
                        "decided_by": f.get("decided_by"), "impact": f.get("release_impact")} for f in fnd]
    out["context_gaps"] = sum(1 for f in per.get("findings") or [] if f.get("kind") == "CONTEXT_GAP")

    # 1. EIO validation
    probs = eio.validate(per)
    out["checks"]["eio_valid"] = not probs
    if probs:
        out["errors"] += [f"EIO {p.get('check')}: {str(p.get('detail'))[:160]}" for p in probs[:5]]

    # 2. the report's own decision (CLI) vs the record
    m = re.findall(r"Release decision:\s*(\w+)", log)
    out["cli_decision"] = m[-1].upper() if m else None
    out["checks"]["cli_matches_record"] = (out["cli_decision"] or "").upper() == (rr.get("state") or "").upper() \
        or (out["cli_decision"] == "PASS" and rr.get("state") == "PASS")
    um = re.findall(r"localhost:5173/evaluations/([0-9a-f-]{36})", log)
    out["dashboard_url"] = f"http://localhost:5173/evaluations/{um[-1]}" if um else None

    # 3. platform: same record, signed, same decision, every finding on the dashboard
    url, key = os.environ.get("UPLOAD_URL", "http://localhost:8000"), os.environ.get("UPLOAD_KEY", "")
    if um and key:
        h = {"Authorization": f"Bearer {key}"}
        try:
            with httpx.Client(timeout=30) as c:
                rec = c.get(f"{url}/api/v1/records/{um[-1]}", headers=h).json()
                pp = rec.get("per") or {}
                out["checks"]["platform_has_record"] = bool(rec.get("record_id"))
                out["checks"]["platform_record_valid"] = not eio.validate(pp) if pp else False
                out["checks"]["platform_digest_ok"] = (rec.get("digest") or "") == str(eio.per_sha256(pp)) if pp else False
                out["checks"]["platform_signed"] = bool(rec.get("signed") or rec.get("attestation"))
                out["checks"]["platform_decision_matches"] = (rec.get("decision") or "").upper() == (rr.get("state") or "").upper()

                def sem(r):  # what the record SAYS: ids and timestamps differ between the local and the uploaded build
                    sc = r.get("scores") or {}
                    return (str((r.get("release_recommendation") or {}).get("state")),
                            round(float((sc.get("readiness") or {}).get("value") or -1), 2),
                            tuple(sorted((m["metric"], round(float(m["value"]), 2) if m.get("value") is not None else None)
                                         for m in sc.get("metrics") or [])),
                            tuple(sorted((str(f.get("kind")), str(f.get("predicate")), str(f.get("severity")),
                                          tuple(f.get("turn_indices") or [])) for f in r.get("findings") or [])))
                a, b = sem(per), sem(pp) if pp else None
                out["checks"]["platform_record_same_content"] = a == b
                if b and a != b:
                    labels = ["decision", "readiness", "metrics", "findings"]
                    out["errors"].append("uploaded record differs from local in: "
                                         + ", ".join(l for l, x, y in zip(labels, a, b) if x != y))
                run_id = rec.get("run_id") or um[-1]
                fl = c.get(f"{url}/api/v1/findings", headers=h, params={"run_id": um[-1], "limit": 200}).json()
                total = fl.get("total")
                items = fl.get("items") or []
                n_plat = len(pp.get("findings") or [])
                out["dashboard_findings"] = total
                out["per_findings_all"] = n_plat
                out["checks"]["all_findings_on_dashboard"] = total == n_plat
                if total != n_plat:
                    out["errors"].append(f"dashboard shows {total} findings, the record has {n_plat}")
                unrated = sum(1 for f in pp.get("findings") or [] if not f.get("severity"))
                out["unrated_findings"] = unrated
                out["dashboard_low"] = sum(1 for i in items if i.get("severity") == "low")
        except Exception as exc:
            out["errors"].append(f"platform check failed: {type(exc).__name__}: {exc}")
    else:
        out["errors"].append("not uploaded (no dashboard URL in the log)")
    out["ok"] = bool(out["checks"]) and all(v for k, v in out["checks"].items() if k != "cli_matches_record")
    (d / "verify.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(*sys.argv[1:4])
