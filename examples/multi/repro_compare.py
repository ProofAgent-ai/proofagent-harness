"""Compare run A vs run B per agent in a reproducibility batch; print a markdown table and write repro_summary.json.

Usage: python repro_compare.py results/repro
Measures, per agent: release decision (PER) A/B, PER readiness delta, local index delta, behavioural score delta,
largest per-metric delta, attack plan identity (same trap per turn), turn questions identical, and PER finding
agreement (Jaccard of (predicate, turn) pairs and of predicates alone, ignoring turn).
"""
import json
from difflib import SequenceMatcher
import sys
from pathlib import Path


def load(d: Path):
    try:
        run = json.loads((d / "run.json").read_text())
        per = json.loads((d / "run.per.json").read_text())
        return run, per
    except Exception:
        return None, None


def jac(a, b):
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def facts(run, per):
    fnd = [f for f in per.get("findings") or [] if f.get("kind") == "BEHAVIOURAL" and f.get("status") == "OPEN"
           and f.get("severity")]
    tr = [t for t in run.get("transcript") or [] if not t.get("repeat_probe")]
    return {
        "decision": (per.get("release_recommendation") or {}).get("state"),
        "per_ready": ((per.get("scores") or {}).get("readiness") or {}).get("value"),
        "index": (run.get("pai") or {}).get("score"),
        "score": run.get("final_score"),
        "metrics": run.get("per_metric") or {},
        "plan": [t.get("trap_name") for t in tr],
        "questions": [t.get("attack_text") or t.get("question") for t in tr],
        "pairs": {(f["predicate"].split(".")[-1], tuple(f.get("turn_indices") or [])) for f in fnd},
        "preds": {f["predicate"].split(".")[-1] for f in fnd},
        "blocking": {f["predicate"].split(".")[-1] for f in fnd if "BLOCK" in str(f.get("release_impact"))},
        "severe": {f["predicate"].split(".")[-1] for f in fnd if f.get("severity") in ("CRITICAL", "HIGH")},
    }


def d(x, y):
    return None if x is None or y is None else round(abs(float(x) - float(y)), 1)


def main(root: str) -> None:
    rows, summ = [], []
    for ag in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        ra, pa = load(ag / "a")
        rb, pb = load(ag / "b")
        if not ra or not rb:
            rows.append(f"| {ag.name} | incomplete | | | | | | | |")
            continue
        a, b = facts(ra, pa), facts(rb, pb)
        mk = set(a["metrics"]) | set(b["metrics"])
        mdel = {k: d(a["metrics"].get(k), b["metrics"].get(k)) for k in mk}
        worst = max(((v, k) for k, v in mdel.items() if v is not None), default=(0, "-"))
        s = {
            "agent": ag.name, "decision_a": a["decision"], "decision_b": b["decision"],
            "decision_same": a["decision"] == b["decision"],
            "per_ready_a": a["per_ready"], "per_ready_b": b["per_ready"], "per_ready_delta": d(a["per_ready"], b["per_ready"]),
            "index_delta": d(a["index"], b["index"]), "score_delta": d(a["score"], b["score"]),
            "worst_metric": worst[1], "worst_metric_delta": worst[0],
            "plan_same": a["plan"] == b["plan"],
            "questions_same": sum(x == y for x, y in zip(a["questions"], b["questions"])),
            "turns": min(len(a["questions"]), len(b["questions"])),
            "text_similarity": round(sum(SequenceMatcher(None, x or "", y or "").ratio()
                                         for x, y in zip(a["questions"], b["questions"])) / max(1, min(len(a["questions"]), len(b["questions"]))), 2),
            "findings_jaccard_turn": round(jac(a["pairs"], b["pairs"]), 2),
            "findings_jaccard_pred": round(jac(a["preds"], b["preds"]), 2),
            "severe_only_a": sorted(a["severe"] - b["severe"]), "severe_only_b": sorted(b["severe"] - a["severe"]),
            "blocking_a": sorted(a["blocking"]), "blocking_b": sorted(b["blocking"]),
        }
        summ.append(s)
        rows.append(
            f"| {ag.name} | {a['decision']}/{b['decision']} {'✓' if s['decision_same'] else '✗'} "
            f"| {a['per_ready']:.1f}/{b['per_ready']:.1f} (Δ{s['per_ready_delta']}) " if a["per_ready"] is not None and b["per_ready"] is not None else
            f"| {ag.name} | {a['decision']}/{b['decision']} {'✓' if s['decision_same'] else '✗'} | n/a ")
        rows[-1] += (f"| Δ{s['score_delta']} | {s['worst_metric']} Δ{s['worst_metric_delta']} "
                     f"| {'same' if s['plan_same'] else 'DIFF'} · {s['questions_same']}/{s['turns']} identical, sim {s['text_similarity']} "
                     f"| {s['findings_jaccard_pred']} / {s['findings_jaccard_turn']} "
                     f"| {', '.join(s['severe_only_a']) or '-'} / {', '.join(s['severe_only_b']) or '-'} |")
    head = ("| Agent | Decision A/B | PER readiness A/B | Behav. score Δ | Worst metric Δ | Plan · attack text "
            "| Findings agree (pred / pred+turn) | Severe only in A / only in B |\n|---|---|---|---|---|---|---|---|")
    print(head)
    print("\n".join(rows))
    done = [s for s in summ]
    if done:
        n = len(done)
        ds = [s["per_ready_delta"] for s in done if s["per_ready_delta"] is not None]
        print(f"\n**{n} agents:** decision agreement {sum(s['decision_same'] for s in done)}/{n}; "
              f"plan identical {sum(s['plan_same'] for s in done)}/{n}; "
              f"PER readiness Δ median {sorted(ds)[len(ds)//2] if ds else 'n/a'}, max {max(ds) if ds else 'n/a'}; "
              f"findings agreement (predicates) mean {sum(s['findings_jaccard_pred'] for s in done)/n:.2f}, "
              f"(predicate+turn) mean {sum(s['findings_jaccard_turn'] for s in done)/n:.2f}.")
    Path(root, "repro_summary.json").write_text(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/repro")
