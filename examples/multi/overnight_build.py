"""Build the overnight plan tracker from results/overnight/state.json (phases, fixes with examples, log)
plus the live rerun batch (results/tracker2 verify.json files). Usage: python overnight_build.py"""
import html
import json
from datetime import datetime, timezone
from pathlib import Path

E = html.escape
ROOT = Path("results/overnight")
STATE = ROOT / "state.json"
OUT = ROOT / "overnight.html"
RERUN = Path("results/tracker2")
AGENTS = ["property-leasing-agent", "code-generation-agent", "medical-triage-assistant", "hr-recruiting-screener",
          "airline-rebooking-agent"]


def pill(s):
    return {"done": "Done", "running": "In progress", "pending": "Queued", "failed": "Failed", "blocked": "Blocked"}.get(s, s)


def main():
    st = json.loads(STATE.read_text())
    phases = "".join(
        f'<li class="ph {p["status"]}"><div class="ph-h"><span class="dot"></span><b>{E(p["title"])}</b>'
        f'<span class="pill {p["status"]}">{pill(p["status"])}</span></div>'
        f'<p>{E(p.get("detail", ""))}</p>'
        + ("".join(f'<div class="note">{E(n)}</div>' for n in p.get("notes", []))) + "</li>"
        for p in st["phases"])
    fixes = "".join(
        f'<article class="fix"><header><span class="tag">{E(f["where"])}</span><h3>{E(f["title"])}</h3>'
        f'<span class="pill {f["status"]}">{pill(f["status"])}</span></header>'
        f'<p>{E(f["why"])}</p><div class="ex"><div><span class="lab">Before</span><pre>{E(f["before"])}</pre></div>'
        f'<div><span class="lab">After</span><pre>{E(f["after"])}</pre></div></div>'
        f'<p class="proof">{E(f.get("proof", ""))}</p></article>'
        for f in st["fixes"])
    sev = "".join(f"<tr><td>{E(r[0])}</td><td>{E(r[1])}</td><td>{E(r[2])}</td><td>{E(r[3])}</td></tr>"
                  for r in st.get("severities", []))
    rows = []
    for ag in AGENTS:
        for rep in "ab":
            v = RERUN / ag / rep / "verify.json"
            cur = (st.get("current_run") or "") == f"{ag}/{rep}"
            if not v.exists():
                rows.append(f'<tr class="{"running" if cur else "queued"}"><td>{E(ag)}</td><td>{rep.upper()}</td>'
                            f'<td colspan="7"><span class="pill {"running" if cur else "pending"}">'
                            f'{"Running now" if cur else "Queued"}</span></td></tr>')
                continue
            d = json.loads(v.read_text())
            m = d.get("metrics") or {}
            f = d.get("findings") or []
            sevs = ", ".join(f"{x['predicate']} ({(x.get('severity') or 'unrated').lower()})" for x in f) or "none"
            rows.append(
                f'<tr><td>{E(ag)}</td><td>{rep.upper()}</td><td><span class="pill d-{(d.get("decision") or "").lower()}">'
                f'{E(d.get("decision") or "?")}</span></td><td class="num">{(d.get("readiness") or 0):.1f}</td>'
                f'<td class="num">{(d.get("turns") or {}).get("selected", "?")}/30</td>'
                f'<td class="num">{"—" if m.get("task-success") is None else f"{m["task-success"]:.0f}"}</td>'
                f'<td class="small">{E(sevs)}</td>'
                f'<td class="num {"good" if d.get("ok") else "bad"}">{sum(1 for x in (d.get("checks") or {}).values() if x)}/'
                f'{len(d.get("checks") or {})}</td><td>{"<a href=\"" + E(d["dashboard_url"]) + "\">open</a>" if d.get("dashboard_url") else "—"}</td></tr>')
    log = "".join(f"<li><time>{E(x[0])}</time> {E(x[1])}</li>" for x in reversed(st.get("log", [])[-40:]))
    done = sum(1 for p in st["phases"] if p["status"] == "done")
    page = TEMPLATE.format(updated=datetime.now(timezone.utc).strftime("%H:%M UTC, %b %d"), phases=phases, fixes=fixes,
                           sev=sev, rows="".join(rows), log=log, done=done, total=len(st["phases"]),
                           pct=round(100 * done / len(st["phases"])), headline=E(st.get("headline", "")),
                           deploy="".join(f"<li>{E(x)}</li>" for x in st.get("deploy", [])))
    OUT.write_text(page)


TEMPLATE = """<title>Overnight Release Run</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Fraunces:opsz,wght@9..144,600&display=swap">
<style>
/* Layout: release runbook. Status strip, phase timeline, fix cards with before/after, severity table, rerun ledger, log. */
:root {{ --bg:#f5f6f3; --panel:#fff; --fg:#1c2220; --muted:#5d6964; --line:#dde2de; --accent:#245f8f;
  --good:#1d7a4a; --goodbg:#e3f3ea; --warn:#a36400; --warnbg:#fbefd9; --bad:#b3261e; --badbg:#fbe4e2; --code:#f0f2ef;
  --display:"Fraunces", Georgia, serif; --body:"IBM Plex Sans", system-ui, sans-serif; --mono:"IBM Plex Mono", ui-monospace, monospace; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#111614; --panel:#192019; --fg:#e3e8e5; --muted:#99a59f; --line:#2b3430; --accent:#7db5e3;
  --good:#6fd39b; --goodbg:#16301f; --warn:#f0b450; --warnbg:#33270f; --bad:#ff8a80; --badbg:#3a1a18; --code:#141a17; color-scheme:dark }} }}
:root[data-theme="dark"] {{ --bg:#111614; --panel:#192019; --fg:#e3e8e5; --muted:#99a59f; --line:#2b3430; --accent:#7db5e3;
  --good:#6fd39b; --goodbg:#16301f; --warn:#f0b450; --warnbg:#33270f; --bad:#ff8a80; --badbg:#3a1a18; --code:#141a17; color-scheme:dark }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.55 var(--body); padding-left:max(16px, env(safe-area-inset-left, 0px));
  padding-right:max(16px, env(safe-area-inset-right, 0px)); padding-block:24px 48px; }}
main {{ max-width:1180px; margin:0 auto; display:grid; gap:28px; }}
h1 {{ font:600 28px/1.15 var(--display); margin:0; text-wrap:balance; }}
h2 {{ font:600 13px var(--body); letter-spacing:.06em; text-transform:uppercase; color:var(--muted); margin:0 0 10px; }}
h3 {{ font:600 15px var(--body); margin:0; flex:1; min-width:0; }}
.lede {{ color:var(--muted); margin:6px 0 0; max-width:80ch; }}
.headline {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--accent); padding:12px 16px; border-radius:8px; }}
.bar {{ height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:10px; }} .bar i {{ display:block; height:100%; background:var(--accent); }}
ol.phases {{ list-style:none; margin:0; padding:0; display:grid; gap:8px; }}
.ph {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px; }}
.ph p {{ margin:4px 0 0 18px; color:var(--muted); }}
.ph-h {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
.dot {{ width:10px; height:10px; border-radius:50%; background:var(--line); flex:none; }}
.ph.done .dot {{ background:var(--good); }} .ph.running .dot {{ background:var(--accent); box-shadow:0 0 0 4px color-mix(in srgb, var(--accent) 25%, transparent); }}
.ph.failed .dot, .ph.blocked .dot {{ background:var(--bad); }}
.ph-h b {{ flex:1; min-width:0; }}
.note {{ margin:6px 0 0 18px; font-size:13px; padding:6px 10px; background:var(--code); border-radius:6px; }}
.pill {{ display:inline-block; padding:1px 9px; border-radius:999px; font-size:12px; font-weight:500; white-space:nowrap; border:1px solid var(--line); color:var(--muted); }}
.pill.done, .pill.d-pass {{ background:var(--goodbg); color:var(--good); border-color:transparent; }}
.pill.running {{ color:var(--accent); border-color:var(--accent); }}
.pill.failed, .pill.blocked, .pill.d-block {{ background:var(--badbg); color:var(--bad); border-color:transparent; }}
.pill.d-review {{ background:var(--warnbg); color:var(--warn); border-color:transparent; }}
.fixes {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 520px), 1fr)); gap:14px; }}
.fix {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; min-width:0; }}
.fix header {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.tag {{ font:500 11px var(--mono); color:var(--accent); }}
.fix p {{ margin:8px 0; }}
.ex {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
@media (max-width:640px) {{ .ex {{ grid-template-columns:1fr; }} }}
.ex > div {{ min-width:0; }}
.lab {{ font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }}
pre {{ margin:4px 0 0; background:var(--code); padding:8px 10px; border-radius:6px; font:12px/1.45 var(--mono); white-space:pre-wrap; word-break:break-word; }}
.proof {{ font-size:13px; color:var(--good); }}
.wrap {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ font-size:11.5px; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); white-space:nowrap; }}
tr:last-child td {{ border-bottom:0; }} tr.queued td {{ color:var(--muted); }}
td.num {{ font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }}
td.small {{ font-size:12px; color:var(--muted); max-width:380px; }}
td.good {{ color:var(--good); }} td.bad {{ color:var(--bad); font-weight:600; }}
ul.log {{ list-style:none; margin:0; padding:12px 14px; background:var(--panel); border:1px solid var(--line); border-radius:10px; max-height:340px; overflow:auto; font-size:13px; }}
ul.log time {{ font-family:var(--mono); color:var(--muted); margin-right:8px; }}
ul.deploy {{ margin:0; padding:12px 14px 12px 32px; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
a {{ color:var(--accent); }}
</style>
<main>
<header>
  <h1>Overnight Release Run</h1>
  <p class="lede">EIO 0.9.0 with the missing severities, five harness fixes, the platform on EIO 0.9, a re-score of yesterday's 20 runs, and 5 high-risk agents re-run A/B at 30 turns, uploaded to the local platform. Updated {updated}. Nothing is committed.</p>
  <div class="bar"><i style="width:{pct}%"></i></div>
</header>
<div class="headline">{headline}</div>
<section><h2>Plan · {done} of {total} phases done</h2><ol class="phases">{phases}</ol></section>
<section><h2>Fixes, with real examples from yesterday's runs</h2><div class="fixes">{fixes}</div></section>
<section><h2>New EIO 0.9 severities (generic-agent baseline)</h2><div class="wrap"><table>
<thead><tr><th>Finding</th><th>Severity</th><th>Release impact</th><th>Applies when</th></tr></thead><tbody>{sev}</tbody></table></div></section>
<section><h2>Re-run: 5 high-risk agents, A and B, 30 turns</h2><div class="wrap"><table>
<thead><tr><th>Agent</th><th>Run</th><th>Decision</th><th>Readiness</th><th>Turns</th><th>Task</th><th>Findings (severity)</th><th>Checks</th><th>Dashboard</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>
<section><h2>Production deploy checklist</h2><ul class="deploy">{deploy}</ul></section>
<section><h2>Activity log</h2><ul class="log">{log}</ul></section>
</main>
"""

if __name__ == "__main__":
    main()
