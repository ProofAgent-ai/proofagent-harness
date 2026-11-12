"""Build the tracker's three data files from a batch folder (one subfolder per agent).

  python build_tracker_data.py results/domains OUT_DIR

Writes OUT_DIR/agents.json (one row per agent), steps.json (one row per agent x harness step, with the decision the
harness made, its time, status and the limitation/gap seen there) and gaps.json (gaps across agents). Every value
comes from the report, the uploaded PER, the phase timings and the run log; agents not run yet show as pending."""
import json
import re
import sys
from pathlib import Path

AGENTS = ["airline-rebooking-agent", "code-generation-agent", "customer-support-agent", "financial-advisor-agent",
          "hr-recruiting-screener", "insurance-claims-agent", "legal-intake-assistant", "medical-triage-assistant",
          "privacy-security-agent", "telecom-account-agent"]
DOMAIN = {"airline-rebooking-agent": "Travel", "code-generation-agent": "Software", "customer-support-agent": "E-commerce",
          "financial-advisor-agent": "Finance", "hr-recruiting-screener": "HR", "insurance-claims-agent": "Insurance",
          "legal-intake-assistant": "Legal", "medical-triage-assistant": "Healthcare",
          "privacy-security-agent": "Privacy", "telecom-account-agent": "Telecom"}
STEPS = [("plan", "Plan"), ("calibrate", "Calibration"), ("context", "Context"), ("conduct", "Conversation"),
         ("jury", "Jury"), ("report", "Score & report"), ("per_export", "PER export"), ("upload", "Upload")]
HERE = Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _j(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def durations(ph):
    marks = (ph or {}).get("phases") or []
    out = {}
    for (name, t), nxt in zip(marks, marks[1:] + [["end", (ph or {}).get("total", 0)]]):
        out[name] = round(nxt[1] - t, 1)
    return out


def upload_result(log: Path) -> dict:
    try:
        text = ANSI.sub("", log.read_text(errors="replace")).replace("\r", "\n")
    except Exception:
        return {}
    url = re.findall(r"https://app\.proofagent\.ai/evaluations/[0-9a-f-]+", text.replace("\n", ""))
    dec = re.findall(r"Release decision:\s*(\w+)", text)
    return {"uploaded": "Uploading PER" in text and bool(dec), "decision": dec[-1] if dec else None,
            "url": url[-1] if url else None, "refused": "Not uploaded" in text}


def _roster(root: Path):
    """The batch's agents: agents.txt in the batch folder ("spec.json name" per line), else the first ten."""
    listing = root / "agents.txt"
    if listing.exists():
        pairs = [ln.split() for ln in listing.read_text().splitlines() if ln.strip()]
        names = [p[1] for p in pairs]
        for spec, name in pairs:
            try:
                DOMAIN.setdefault(name, json.loads((HERE.parent / "agents" / spec).read_text()).get("domain", "").replace("_", " ").title())
            except Exception:
                DOMAIN.setdefault(name, "")
        return names
    return AGENTS


def build(root: Path):
    agents, steps, gaps = [], [], {}

    def gap(key, step, text, fix, sev, agent):
        g = gaps.setdefault(key, {"gap": text, "step": step, "severity": sev, "fix": fix, "agents": []})
        if agent not in g["agents"]:
            g["agents"].append(agent)

    for name in _roster(root):
        f = root / name
        rep, per, ph = _j(f / "run.json"), _j(f / "run.per.json"), _j(f / "phases.json")
        up = upload_result(f / "run.log")
        dur = durations(ph)
        if not rep and ph and ph.get("exit") not in (0, 2, None):
            # The run ended without a report: mark the step it stopped in.
            reached = [k for k, _ in ph.get("phases") or []]
            agents.append({"agent": name, "domain": DOMAIN[name], "status": "failed",
                           "withheld": "whole run", "uploaded": "no", "seconds": int(ph.get("total") or 0)})
            stopped = False
            for key, label in STEPS:
                if stopped:
                    steps.append({"agent": name, "step": label, "status": "pending", "seconds": None, "decision": "", "gap": ""})
                    continue
                if key in reached and key != reached[-1]:
                    steps.append({"agent": name, "step": label, "status": "ok", "seconds": dur.get(key), "decision": "completed", "gap": ""})
                elif key in reached or (label == "Context" and reached and reached[-1] == "calibrate"):
                    mins = round((ph.get("total") or 0) / 60)
                    steps.append({"agent": name, "step": label, "status": "fail", "seconds": ph.get("total"),
                                  "decision": "stopped: no answer from the local model",
                                  "gap": f"hung {mins} min on one model call (runaway generation, no timeout); run stopped and queued again"})
                    gap("llm_hang", label, "A model call hung with no timeout", "Per-call timeout (added: PROOFAGENT_LLM_TIMEOUT, 300 s)", "high", name)
                    stopped = True
                else:
                    steps.append({"agent": name, "step": label, "status": "pending", "seconds": None, "decision": "", "gap": ""})
            continue
        if not rep:
            running = (f / "run.log").exists()
            agents.append({"agent": name, "domain": DOMAIN[name], "status": "running" if running else "pending"})
            for key, label in STEPS:
                seen = key in dur or key in dict((ph or {}).get("phases") or [])
                steps.append({"agent": name, "step": label, "status": "running" if running and seen else "pending",
                              "seconds": None, "decision": "", "gap": ""})
            continue
        m = rep.get("metadata") or {}
        verdicts = rep.get("check_verdicts") or []
        applicable = [v for v in verdicts if v.get("observed") is not None]
        code = sum(1 for v in verdicts if v.get("decided_by") == "code")
        sc = (per or {}).get("scores") or {}
        axes = {a["axis"].rsplit(".", 1)[-1]: a.get("value") for a in sc.get("axes") or []}
        withheld_m = [x["metric"].rsplit(".", 1)[-1] for x in sc.get("metrics") or [] if x.get("status") != "MEASURED"]
        jury_fail = sum(1 for t in rep.get("technical_issues") or [] if "Harness LLM" in str(t.get("headline")))
        uneval = [k for k in (m.get("metrics") or []) if k not in (rep.get("per_metric") or {})]
        low = [w for w in rep.get("warnings") or [] if w.startswith("Low evidence")]
        echo = any("system-prompt echo" in str(t.get("headline")) for t in rep.get("technical_issues") or [])
        rec, ran = m.get("turns_recommended"), len(rep.get("transcript") or [])
        readiness = (sc.get("readiness") or {}).get("value")
        rr = (per or {}).get("release_recommendation") or {}
        agents.append({
            "agent": name, "domain": DOMAIN[name], "status": "done",
            "score": round(float(rep.get("final_score") or 0) * 10), "certification": rep.get("certification"),
            "decision": up.get("decision") or rr.get("state"), "readiness": None if readiness is None else round(readiness),
            "context": axes.get("context"), "behaviour": axes.get("behaviour"), "compliance": axes.get("compliance"),
            "governance": axes.get("governance"), "turns_recommended": rec, "turns_run": ran,
            "seconds": int(rep.get("duration_seconds") or 0), "calls": m.get("llm_call_count"),
            "tokens_in": rep.get("primary_prompt_tokens"), "tokens_out": rep.get("primary_completion_tokens"),
            "checks": len(verdicts), "checks_code": code, "applicable": len(applicable),
            "jury_failures": jury_fail, "withheld": ", ".join(withheld_m + uneval) or "none",
            "uploaded": "yes" if up.get("uploaded") else "no", "url": up.get("url"),
        })

        def row(label, status, seconds, decision, g=""):
            steps.append({"agent": name, "step": label, "status": status, "seconds": seconds, "decision": decision, "gap": g})

        # Plan
        g = ""
        if rec and ran < rec:
            g = f"ran {ran} of {rec} recommended turns"
            gap("turns_short", "Plan", "Fewer turns than the planner recommends", "Use --adaptive-turns everywhere", "medium", name)
        row("Plan", "warn" if g else "ok", dur.get("plan"),
            f"{m.get('turns_mode') or 'fixed'} turns: {ran} (recommended {rec}); domains {', '.join(m.get('domains_inferred') or []) or '—'}", g)
        # Calibration
        row("Calibration", "ok", dur.get("calibrate"),
            f"agent {m.get('agent_class') or '—'} (determinism {m.get('agent_determinism')}); transcript {m.get('transcript_source') or 'fresh'}; jury check skipped")
        # Context
        ctx = axes.get("context")
        g, st = "", "ok"
        if ctx is None:
            st, g = "fail", "context not measured: readiness withheld"
            gap("context_missing", "Context", "Context axis withheld", "Always assess context; template when none supplied", "high", name)
        elif ctx < 50:
            st, g = "warn", f"weak context ({round(ctx)}%)"
            gap("context_weak", "Context", "Weak agent context (prompt, tools, guardrails)", "Report as a top fix for the agent owner", "medium", name)
        row("Context", st, dur.get("context"), f"context axis {ctx if ctx is None else round(ctx)}", g)
        # Conversation
        g = "agent may echo its system prompt" if echo else ""
        if echo:
            gap("prompt_echo", "Conversation", "Possible system-prompt echo", "Add a code check for prompt leakage", "medium", name)
        row("Conversation", "warn" if echo else "ok", dur.get("conduct"), f"{ran} turns conducted", g)
        # Jury
        gs = []
        if jury_fail:
            gs.append("harness LLM errors")
            gap("jury_errors", "Jury", "Local jury model returned errors / malformed votes", "Fallback jury model; JSON preflight", "high", name)
        if uneval:
            gs.append(f"{', '.join(uneval)} not scored")
            gap("metric_unscored", "Jury", "A metric could not be scored", "Retry with fallback model; never leave empty", "high", name)
        null_share = 1 - len(applicable) / len(verdicts) if verdicts else 0
        if null_share > 0.6:
            gs.append(f"{int(null_share * 100)}% of checks not applicable")
            gap("sparse_checks", "Jury", "Most checks not applicable (thin evidence)", "More turns; control turns for task success", "medium", name)
        row("Jury", "fail" if uneval else "warn" if gs else "ok", dur.get("jury"),
            f"{len(verdicts)} checks · {code} by code · {len(applicable)} applicable · {m.get('consensus_strategy')}", "; ".join(gs))
        # Score
        g = "; ".join(w.split(". ")[0].replace("Low evidence: ", "low evidence: ") for w in low)
        if low:
            gap("low_evidence", "Score & report", "Metric scored from fewer than 3 checks", "Raise turns / add control turns", "medium", name)
        row("Score & report", "warn" if low else "ok", dur.get("report"),
            f"score {round(float(rep.get('final_score') or 0) * 10)} · {rep.get('certification')}", g)
        # PER
        gs, st = [], "ok"
        if readiness is None:
            st = "fail"; gs.append("readiness withheld")
            gap("readiness_withheld", "PER export", "Readiness withheld", "Fill every axis (template defaults)", "high", name)
        if withheld_m:
            st = "warn" if st == "ok" else st; gs.append(f"withheld: {', '.join(withheld_m)}")
            gap("evaluator_metrics", "PER export", "Evaluator-reliability metrics withheld", "Emit self-check predicates into the PER", "medium", name)
        row("PER export", st, dur.get("per_export"),
            f"readiness {readiness if readiness is None else round(readiness)} · {rr.get('state')} · {len((per or {}).get('findings') or [])} findings", "; ".join(gs))
        # Upload
        st = "ok" if up.get("uploaded") else "fail"
        if not up.get("uploaded"):
            gap("upload_failed", "Upload", "Upload did not complete", "Check the run log", "high", name)
        row("Upload", st, dur.get("upload"), f"{up.get('decision') or '—'} · {'uploaded' if up.get('uploaded') else 'not uploaded'}",
            "" if up.get("uploaded") else "not uploaded")
    gl = sorted(gaps.values(), key=lambda g: ({"high": 0, "medium": 1, "low": 2}[g["severity"]], -len(g["agents"])))
    for g in gl:
        g["count"] = len(g["agents"]); g["agents"] = ", ".join(g["agents"])
    return agents, steps, gl


def main():
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    a, s, g = build(root)
    (out / "agents.json").write_text(json.dumps(a, indent=1))
    (out / "steps.json").write_text(json.dumps(s, indent=1))
    (out / "gaps.json").write_text(json.dumps(g, indent=1))
    print(f"{len(a)} agents ({sum(1 for x in a if x['status'] == 'done')} done), {len(s)} step rows, {len(g)} gaps")


if __name__ == "__main__":
    main()
