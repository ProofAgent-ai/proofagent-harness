"""End-to-end decision tracker for a batch of harness runs: one row per agent, every decision the harness made.

  python track_runs.py results/domains            # prints a markdown table, writes tracker.json beside it

Reads, per agent folder: run.json (report), run.per.json (the PER that was uploaded), phases.json (phase timings
from phase_timer.py). Nothing is recomputed: every value is what the harness or the PER recorded."""
import json
import sys
from collections import Counter
from pathlib import Path


def _j(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def phase_durations(ph: dict | None) -> dict[str, float]:
    if not ph:
        return {}
    marks = ph.get("phases") or []
    out: dict[str, float] = {}
    for (name, t), nxt in zip(marks, marks[1:] + [["end", ph.get("total", 0)]]):
        out[name] = round(nxt[1] - t, 1)
    return out


def track(folder: Path) -> dict:
    rep, per, ph = _j(folder / "run.json"), _j(folder / "run.per.json"), _j(folder / "phases.json")
    row: dict = {"agent": folder.name}
    if not rep:
        row["status"] = "no report"
        return row
    m = rep.get("metadata") or {}
    verdicts = rep.get("check_verdicts") or []
    decided = Counter(v.get("decided_by") for v in verdicts)
    applicable = [v for v in verdicts if v.get("observed") is not None]
    failed = [v for v in applicable if v.get("observed") is True]   # risk checks true / positive handled by EIO
    row.update({
        # 1. plan
        "turns_mode": m.get("turns_mode"), "turns_recommended": m.get("turns_recommended"),
        "turns_run": len(rep.get("transcript") or []), "domains": m.get("domains_inferred"),
        "transcript_source": m.get("transcript_source"),
        # 2. calibration
        "agent_class": m.get("agent_class"), "agent_determinism": m.get("agent_determinism"),
        # 3. context / governance inputs
        "context_score": (rep.get("context_engineering") or {}).get("overall") if isinstance(rep.get("context_engineering"), dict) else None,
        "governance_profile": m.get("governance_profile_name") or m.get("governance_profile_source"),
        # 4. jury
        "consensus": m.get("consensus_strategy"), "checks": len(verdicts), "checks_applicable": len(applicable),
        "decided_code": decided.get("code", 0), "decided_llm": decided.get("llm", 0),
        "null_share": round(1 - len(applicable) / len(verdicts), 2) if verdicts else None,
        "jury_failures": sum(1 for t in rep.get("technical_issues") or [] if "Harness LLM" in str(t.get("headline"))),
        "unevaluated_metrics": [k for k in (m.get("metrics") or []) if k not in (rep.get("per_metric") or {})],
        "low_evidence": [w for w in rep.get("warnings") or [] if w.startswith("Low evidence")],
        # 5. outcome
        "score": round(float(rep.get("final_score") or 0) * 10), "certification": rep.get("certification"),
        "per_metric": {k: round(v, 1) for k, v in (rep.get("per_metric") or {}).items()},
        # 6. cost / speed
        "seconds": int(rep.get("duration_seconds") or 0), "phases": phase_durations(ph),
        "harness_calls": m.get("llm_call_count"), "tokens": rep.get("tokens_used"),
        "prompt_tokens": rep.get("primary_prompt_tokens"), "completion_tokens": rep.get("primary_completion_tokens"),
    })
    if per:
        sc = per.get("scores") or {}
        axes = {a["axis"].rsplit(".", 1)[-1]: a.get("value") for a in sc.get("axes") or []}
        withheld_metrics = [x["metric"].rsplit(".", 1)[-1] for x in sc.get("metrics") or [] if x.get("status") != "MEASURED"]
        rr = per.get("release_recommendation") or {}
        row.update({
            "per_axes": axes, "per_readiness": (sc.get("readiness") or {}).get("value"),
            "per_readiness_status": (sc.get("readiness") or {}).get("status"),
            "per_withheld_metrics": withheld_metrics,
            "per_withheld_axes": [a for a, v in axes.items() if v is None],
            "per_decision": rr.get("state"),
            "per_decisive": [d.get("id") for d in rr.get("decisive") or []],
            "per_findings": len(per.get("findings") or []),
            "per_limitations": [str(x.get("code") or x.get("id") or x)[:60] for x in per.get("limitations") or []][:6],
        })
    return row


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "results/domains")
    rows = [track(d) for d in sorted(p for p in root.iterdir() if p.is_dir())]
    (root / "tracker.json").write_text(json.dumps(rows, indent=1, default=str))
    print("| Agent | Turns (rec→run) | Context | Checks code/llm | Null % | Jury fails | Withheld | Score | PER decision | Readiness | Time s | Calls | Tokens in/out |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "score" not in r:
            print(f"| {r['agent']} | {r.get('status')} |" + " |" * 11)
            continue
        withheld = (r.get("per_withheld_axes") or []) + (r.get("unevaluated_metrics") or [])
        print(f"| {r['agent']} | {r['turns_recommended']}→{r['turns_run']} | {r.get('per_axes', {}).get('context')} | "
              f"{r['decided_code']}/{r['decided_llm']} | {int((r['null_share'] or 0) * 100)} | {r['jury_failures']} | "
              f"{', '.join(withheld) or '—'} | {r['score']} {r['certification']} | {r.get('per_decision')} | "
              f"{r.get('per_readiness')} | {r['seconds']} | {r['harness_calls']} | "
              f"{(r['prompt_tokens'] or 0) // 1000}K/{(r['completion_tokens'] or 0) // 1000}K |")


if __name__ == "__main__":
    main()
