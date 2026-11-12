"""Build the tracker page (HTML) from a tracker batch: python tracker_build.py results/tracker out.html"""
import html
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENTS = ("customer-support-agent banking-dispute-agent medical-triage-assistant hotel-reservations-agent "
          "utility-billing-agent property-leasing-agent privacy-security-agent airline-rebooking-agent "
          "hr-recruiting-screener code-generation-agent").split()
METRICS = ["safety", "instruction-following", "manipulation-resistance", "hallucination-resistance", "task-success",
           "tool-use"]
SHORT = {"safety": "Safety", "instruction-following": "Instr.", "manipulation-resistance": "Manip.",
         "hallucination-resistance": "Halluc.", "task-success": "Task", "tool-use": "Tools"}
CHECKS = {"eio_valid": "PER valid (EIO)", "platform_has_record": "On platform", "platform_record_valid": "Stored record valid",
          "platform_digest_ok": "Digest intact", "platform_signed": "Signed", "platform_decision_matches": "Decision matches",
          "platform_record_same_content": "Same content as local", "all_findings_on_dashboard": "All findings on dashboard"}
E = html.escape


def feed(agent: str) -> list[str]:
    d = Path("context") / agent
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()) if d.exists() else []


def spec_domain(agent: str) -> str:
    p = Path("../agents") / f"{agent.replace('-', '_')}.json"
    try:
        j = json.loads(p.read_text())
        return j.get("domain") or j.get("industry") or ""
    except Exception:
        return ""


def fmt(v, nd=0):
    return "—" if v is None else f"{v:.{nd}f}"


def cls(v):
    if v is None:
        return "na"
    return "good" if v >= 90 else "warn" if v >= 70 else "bad"


def jac(a, b):
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def main(root: str, out: str) -> None:
    R = Path(root)
    status = {}
    try:
        status = json.loads((R / "status.json").read_text())
    except Exception:
        pass
    runs = {}
    for ag in AGENTS:
        for rep in "ab":
            v = R / ag / rep / "verify.json"
            runs[(ag, rep)] = json.loads(v.read_text()) if v.exists() else None
    done = [r for r in runs.values() if r]
    durs = [r.get("duration_min") or 0 for r in done if r.get("duration_min")]
    avg = statistics.mean(durs) if durs else 30
    left = sum(1 for r in runs.values() if not r)
    cur = status.get("current") or ""
    now = datetime.now(timezone.utc)
    eta_h = left * avg / 60
    ok = sum(1 for r in done if r.get("ok"))

    # ---- run table
    rows = []
    for ag in AGENTS:
        dom = spec_domain(ag)
        files = feed(ag)
        for rep in "ab":
            r = runs[(ag, rep)]
            key = f"{ag}/{rep}"
            if not r:
                st = "running" if key == cur and status.get("state") == "running" else "queued"
                rows.append(f'<tr class="{st}"><td class="ag">{E(ag)}</td><td>{rep.upper()}</td><td>{E(dom)}</td>'
                            f'<td class="files">{E(", ".join(files))}</td><td colspan="{len(METRICS) + 6}">'
                            f'<span class="pill {st}">{"Running now" if st == "running" else "Queued"}</span></td></tr>')
                continue
            m = r.get("metrics") or {}
            mcells = "".join(f'<td class="num {cls(m.get(k))}" title="{E(str(r.get("withheld", {}).get(k) or ""))}">'
                             f'{fmt(m.get(k))}</td>' for k in METRICS)
            fs = r.get("findings") or []
            sev = {}
            for f in fs:
                sev[f.get("severity") or "UNRATED"] = sev.get(f.get("severity") or "UNRATED", 0) + 1
            fsum = " ".join(f'<span class="sev {s.lower()}">{n} {s.lower()}</span>' for s, n in sorted(sev.items())) or "none"
            checks = r.get("checks") or {}
            npass = sum(1 for k in CHECKS if checks.get(k))
            vcls = "good" if r.get("ok") else "bad"
            dec = r.get("decision") or "?"
            cli = r.get("cli_decision") or "?"
            link = r.get("dashboard_url")
            dom2 = ", ".join(r.get("domains") or []) or dom
            rows.append(
                f'<tr><td class="ag">{E(ag)}</td><td>{rep.upper()}</td><td>{E(dom2)}</td>'
                f'<td class="files">{E(", ".join(files))}</td>'
                f'<td><span class="pill d-{dec.lower()}">{E(dec)}</span>'
                + (f'<div class="sub">CLI {E(cli)}</div>' if cli != dec else "") + "</td>"
                f'<td class="num {cls(r.get("readiness"))}">{fmt(r.get("readiness"), 1)}</td>{mcells}'
                f'<td>{fsum}</td><td class="num {vcls}">{npass}/{len(CHECKS)}</td>'
                f'<td class="num">{fmt(r.get("duration_min"))}m</td>'
                f'<td>{f"<a href=\"{E(link)}\">open</a>" if link else "—"}</td></tr>')

    # ---- reproducibility
    rep_rows = []
    gaps, agree, fj = [], 0, []
    for ag in AGENTS:
        a, b = runs[(ag, "a")], runs[(ag, "b")]
        if not (a and b):
            continue
        same = a.get("decision") == b.get("decision")
        agree += same
        g = abs((a.get("readiness") or 0) - (b.get("readiness") or 0))
        gaps.append(g)
        ma, mb = a.get("metrics") or {}, b.get("metrics") or {}
        md = {k: abs(ma[k] - mb[k]) for k in METRICS if ma.get(k) is not None and mb.get(k) is not None}
        worst = max(md.items(), key=lambda x: x[1]) if md else ("—", 0)
        pa = {(f["predicate"], f.get("severity")) for f in a.get("findings") or []}
        pb = {(f["predicate"], f.get("severity")) for f in b.get("findings") or []}
        j = jac({p for p, _ in pa}, {p for p, _ in pb})
        fj.append(j)
        plan = "same" if a.get("plan") == b.get("plan") else "differs"
        only_a = sorted({p for p, _ in pa} - {p for p, _ in pb})
        only_b = sorted({p for p, _ in pb} - {p for p, _ in pa})
        rep_rows.append(
            f'<tr><td class="ag">{E(ag)}</td><td><span class="pill {"good" if same else "bad"}">'
            f'{E(a.get("decision") or "?")} / {E(b.get("decision") or "?")}</span></td>'
            f'<td class="num">{fmt(a.get("readiness"), 1)} / {fmt(b.get("readiness"), 1)}</td>'
            f'<td class="num {"good" if g < 2 else "warn" if g < 5 else "bad"}">{g:.1f}</td>'
            f'<td>{E(SHORT.get(worst[0], worst[0]))} {worst[1]:.0f}</td><td>{plan}</td>'
            f'<td class="num {"good" if j >= .8 else "warn" if j >= .5 else "bad"}">{j:.2f}</td>'
            f'<td class="small">{E(", ".join(only_a) or "–")} / {E(", ".join(only_b) or "–")}</td></tr>')

    # ---- findings across runs
    agg = {}
    for (ag, rep), r in runs.items():
        for f in (r or {}).get("findings") or []:
            k = (f["predicate"], f.get("severity") or "UNRATED")
            e = agg.setdefault(k, {"n": 0, "runs": set(), "decided": set(), "risk": f.get("risk")})
            e["n"] += 1
            e["runs"].add(f"{ag}/{rep.upper()}")
            e["decided"].add(f.get("decided_by") or "?")
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNRATED": 4}
    frows = "".join(
        f'<tr><td>{E(p)}</td><td><span class="sev {s.lower()}">{s.lower()}</span></td><td>{E(e["risk"] or "")}</td>'
        f'<td class="num">{e["n"]}</td><td class="num">{len(e["runs"])}</td><td>{E(", ".join(sorted(e["decided"])))}</td>'
        f'<td class="small">{E(", ".join(sorted(e["runs"])))}</td></tr>'
        for (p, s), e in sorted(agg.items(), key=lambda x: (order.get(x[0][1], 9), -x[1]["n"])))

    # ---- metric averages
    mavg = []
    for k in METRICS:
        vals = [(r.get("metrics") or {}).get(k) for r in done]
        vals = [v for v in vals if v is not None]
        mavg.append((k, statistics.mean(vals) if vals else None, len(vals)))
    bars = "".join(
        f'<div class="bar"><span class="bl">{SHORT[k]}</span><span class="track"><span class="fill {cls(v)}" '
        f'style="width:{(v or 0):.0f}%"></span></span><span class="bv">{fmt(v)}<small> n={n}</small></span></div>'
        for k, v, n in mavg)

    # ---- verification issues
    issues = []
    for (ag, rep), r in runs.items():
        if not r:
            continue
        for k, lab in CHECKS.items():
            if (r.get("checks") or {}).get(k) is False:
                issues.append(f"{ag}/{rep.upper()}: {lab} failed")
        for e in r.get("errors") or []:
            issues.append(f"{ag}/{rep.upper()}: {e}")
        if r.get("cli_decision") and r.get("decision") and r["cli_decision"] != r["decision"]:
            issues.append(f"{ag}/{rep.upper()}: CLI decision {r['cli_decision']} is stricter than the record's "
                          f"{r['decision']} (behaviour floor)")
        if r.get("unrated_findings"):
            issues.append(f"{ag}/{rep.upper()}: {r['unrated_findings']} finding(s) have no EIO severity; the dashboard "
                          f"lists them as low")
    issue_html = "".join(f"<li>{E(i)}</li>" for i in issues[:80]) or "<li>No verification problems so far.</li>"
    analysis = Path(root, "analysis.html")
    analysis_html = analysis.read_text() if analysis.exists() else ""

    page = TEMPLATE.format(
        updated=now.strftime("%H:%M UTC, %b %d"), done=len(done), total=len(runs), ok=ok,
        current=E(cur or ("finished" if status.get("state") == "done" else "—")),
        eta=("done" if not left else f"~{eta_h:.1f} h left"), avg=f"{avg:.0f}",
        agree=(f"{agree}/{len(gaps)}" if gaps else "—"),
        gap=(f"{statistics.median(gaps):.1f}" if gaps else "—"), gapmax=(f"{max(gaps):.1f}" if gaps else "—"),
        fj=(f"{statistics.mean(fj):.2f}" if fj else "—"),
        mheads="".join(f"<th>{SHORT[k]}</th>" for k in METRICS), rows="".join(rows),
        reprows="".join(rep_rows) or '<tr><td colspan="8" class="empty">Appears once an agent has both runs.</td></tr>',
        frows=frows or '<tr><td colspan="7" class="empty">No findings yet.</td></tr>', bars=bars,
        issues=issue_html, nissues=len(issues), analysis=analysis_html, pct=f"{100 * len(done) / len(runs):.0f}")
    Path(out).write_text(page)


TEMPLATE = """<title>Reproducibility Tracker</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Fraunces:opsz,wght@9..144,600&display=swap">
<style>
/* Layout: one lab-notebook column; status strip, then run ledger, reproducibility, metrics, findings, checks. */
:root {{ --bg:#f6f7f5; --panel:#ffffff; --fg:#1d2321; --muted:#5f6b66; --line:#dfe3e0; --accent:#1f6f5c;
  --good:#1d7a4a; --goodbg:#e3f3ea; --warn:#a36400; --warnbg:#fbefd9; --bad:#b3261e; --badbg:#fbe4e2; --na:#8a938f;
  --display:"Fraunces", Georgia, serif; --body:"IBM Plex Sans", system-ui, sans-serif; --mono:"IBM Plex Mono", ui-monospace, monospace; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#121614; --panel:#1a201d; --fg:#e4e9e6; --muted:#9aa6a0; --line:#2c3531; --accent:#5fc3a4;
  --good:#6fd39b; --goodbg:#16301f; --warn:#f0b450; --warnbg:#33270f; --bad:#ff8a80; --badbg:#3a1a18; --na:#7c8782; color-scheme:dark }} }}
:root[data-theme="dark"] {{ --bg:#121614; --panel:#1a201d; --fg:#e4e9e6; --muted:#9aa6a0; --line:#2c3531; --accent:#5fc3a4;
  --good:#6fd39b; --goodbg:#16301f; --warn:#f0b450; --warnbg:#33270f; --bad:#ff8a80; --badbg:#3a1a18; --na:#7c8782; color-scheme:dark }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.5 var(--body); padding-left:max(16px, env(safe-area-inset-left, 0px));
  padding-right:max(16px, env(safe-area-inset-right, 0px)); padding-block:24px 48px; }}
main {{ max-width:1400px; margin:0 auto; display:grid; gap:28px; }}
h1 {{ font:600 28px/1.15 var(--display); margin:0; text-wrap:balance; }}
h2 {{ font:600 13px var(--body); letter-spacing:.06em; text-transform:uppercase; color:var(--muted); margin:0 0 10px; }}
p.lede {{ margin:6px 0 0; color:var(--muted); max-width:75ch; }}
.strip {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
.strip > div {{ background:var(--panel); padding:12px 14px; min-width:0; }}
.strip b {{ display:block; font:500 20px var(--mono); font-variant-numeric:tabular-nums; }}
.strip span {{ color:var(--muted); font-size:12px; }}
.progress {{ height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:10px; }}
.progress i {{ display:block; height:100%; background:var(--accent); }}
.wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--panel); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ font-weight:600; font-size:11.5px; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); white-space:nowrap; position:sticky; top:0; background:var(--panel); }}
tr:last-child td {{ border-bottom:0; }}
td.num {{ font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }}
td.ag {{ font-weight:500; white-space:nowrap; }}
td.files, td.small {{ color:var(--muted); font-size:12px; max-width:240px; }}
td.good {{ color:var(--good); }} td.warn {{ color:var(--warn); }} td.bad {{ color:var(--bad); font-weight:600; }} td.na {{ color:var(--na); }}
tr.queued td {{ color:var(--na); }}
tr.running td {{ background:color-mix(in srgb, var(--accent) 8%, transparent); }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; font-weight:500; white-space:nowrap; border:1px solid var(--line); }}
.pill.running {{ border-color:var(--accent); color:var(--accent); }}
.pill.good, .pill.d-pass {{ background:var(--goodbg); color:var(--good); border-color:transparent; }}
.pill.d-review {{ background:var(--warnbg); color:var(--warn); border-color:transparent; }}
.pill.bad, .pill.d-block {{ background:var(--badbg); color:var(--bad); border-color:transparent; }}
.sub {{ font-size:11px; color:var(--bad); margin-top:2px; }}
.sev {{ display:inline-block; font-size:11.5px; padding:0 6px; border-radius:4px; margin:0 3px 3px 0; white-space:nowrap; background:var(--line); }}
.sev.critical {{ background:var(--badbg); color:var(--bad); font-weight:600; }}
.sev.high {{ background:var(--badbg); color:var(--bad); }}
.sev.medium {{ background:var(--warnbg); color:var(--warn); }}
.sev.unrated {{ background:transparent; border:1px dashed var(--na); color:var(--muted); }}
.bars {{ display:grid; gap:8px; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.bar {{ display:grid; grid-template-columns:70px 1fr 90px; gap:10px; align-items:center; }}
.bl {{ color:var(--muted); font-size:12.5px; }}
.track {{ height:10px; background:var(--line); border-radius:5px; overflow:hidden; }}
.fill {{ display:block; height:100%; }} .fill.good {{ background:var(--good); }} .fill.warn {{ background:var(--warn); }} .fill.bad {{ background:var(--bad); }}
.bv {{ font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right; }} .bv small {{ color:var(--muted); }}
.cols {{ display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 2fr); gap:20px; }}
@media (max-width: 860px) {{ .cols {{ grid-template-columns:1fr; }} }}
ul.issues {{ margin:0; padding:12px 14px 12px 32px; background:var(--panel); border:1px solid var(--line); border-radius:10px; max-height:360px; overflow:auto; }}
ul.issues li {{ margin:2px 0; font-size:13px; }}
.empty {{ color:var(--muted); text-align:center; padding:18px; }}
a {{ color:var(--accent); }} a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
.analysis {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 20px; max-width:110ch; }}
.analysis:empty {{ display:none; }}
.legend {{ color:var(--muted); font-size:12px; margin-top:6px; }}
</style>
<main>
<header>
  <h1>Reproducibility Tracker</h1>
  <p class="lede">10 agents, each evaluated twice (run A and run B) with the same seed, 30 turns per run, local models in LM Studio
  (judge Gemma 3 12B, agent Qwen3.5-9B). Every PER is uploaded to the local platform and checked. Updated {updated}.</p>
  <div class="progress" aria-label="progress"><i style="width:{pct}%"></i></div>
</header>
<section class="strip" aria-label="status">
  <div><b>{done}/{total}</b><span>runs finished</span></div>
  <div><b>{current}</b><span>running now</span></div>
  <div><b>{eta}</b><span>about {avg} min per run</span></div>
  <div><b>{ok}/{done}</b><span>runs passing every platform check</span></div>
  <div><b>{agree}</b><span>agents with the same decision in A and B</span></div>
  <div><b>{gap}</b><span>median readiness gap A–B (max {gapmax})</span></div>
  <div><b>{fj}</b><span>findings agreement A–B (1 = same)</span></div>
</section>
<section class="analysis">{analysis}</section>
<section>
  <h2>Runs</h2>
  <div class="wrap"><table>
    <thead><tr><th>Agent</th><th>Run</th><th>Domain</th><th>Feed files</th><th>Decision</th><th>Readiness</th>{mheads}<th>Findings</th><th>Checks</th><th>Time</th><th>Dashboard</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
  <p class="legend">Scores are the PER's, 0–100. A dash means the metric was not measured (hover for the reason). Checks: PER valid, on the platform, stored record valid, digest intact, signed, same decision, same content as local, all findings on the dashboard. Dashboard links open the local portal (localhost:5173).</p>
</section>
<section>
  <h2>Reproducibility, run A vs run B</h2>
  <div class="wrap"><table>
    <thead><tr><th>Agent</th><th>Decision A / B</th><th>Readiness A / B</th><th>Gap</th><th>Largest metric gap</th><th>Plan</th><th>Findings agree</th><th>Only in A / only in B</th></tr></thead>
    <tbody>{reprows}</tbody></table></div>
</section>
<section class="cols">
  <div><h2>Average score per metric</h2><div class="bars">{bars}</div></div>
  <div><h2>Verification issues ({nissues})</h2><ul class="issues">{issues}</ul></div>
</section>
<section>
  <h2>Findings across all runs</h2>
  <div class="wrap"><table>
    <thead><tr><th>Finding</th><th>Severity</th><th>Risk</th><th>Count</th><th>Runs</th><th>Decided by</th><th>Where</th></tr></thead>
    <tbody>{frows}</tbody></table></div>
</section>
</main>
"""

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/tracker",
         sys.argv[2] if len(sys.argv) > 2 else "results/tracker/tracker.html")
