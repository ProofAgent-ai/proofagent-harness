"""Render a saved ProofAgent report (.json) as a simple, self-contained HTML
dashboard you can open in a browser — no server, no internet, no deps.

    python examples/report_viewer.py results/<report>.json
    python examples/report_viewer.py results/<report>.json --open   # also open it

It embeds the report JSON directly in the HTML, so the output file works
offline and survives being moved/emailed. Renders: headline + certification,
per-metric bars, executive brief, the full transcript, a turns x metrics
PASS/FAIL audit matrix, the per-persona jury debate (both rounds), findings,
token/timing KPIs, and the raw JSON. Cost is intentionally not shown.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--bg:#0b1020;--card:#121a31;--card2:#0f1729;--line:#23314f;--txt:#e6ecf7;--mut:#8aa0c6;--cyan:#22d3ee;--violet:#a78bfa;--amber:#fbbf24;--green:#34d399;--red:#f87171;}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  a{color:var(--cyan)} code{font-family:"Fira Code",Monaco,monospace;font-size:12px}
  .wrap{max-width:1100px;margin:0 auto;padding:24px}
  .hdr{display:flex;flex-wrap:wrap;align-items:center;gap:18px;border:1px solid var(--line);background:linear-gradient(135deg,#101a33,#0b1020);border-radius:16px;padding:20px 24px}
  .score{font-size:48px;font-weight:800;line-height:1} .score small{font-size:18px;color:var(--mut);font-weight:600}
  .badge{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:700;font-size:12px;letter-spacing:.5px}
  .b-gold{background:#3b2f0b;color:#fcd34d;border:1px solid #6b4e0e}
  .b-silver{background:#26324d;color:#cbd5e1;border:1px solid #46566f}
  .b-other{background:#3a1f22;color:#fca5a5;border:1px solid #6b2b2f}
  .pill{background:#0c1428;border:1px solid var(--line);border-radius:10px;padding:4px 10px;color:var(--mut);font-size:12px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .kpi .v{font-size:22px;font-weight:800} .kpi .l{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.6px;margin-top:2px}
  .tabs{display:flex;flex-wrap:wrap;gap:6px;margin:22px 0 14px;border-bottom:1px solid var(--line)}
  .tab{padding:9px 16px;cursor:pointer;color:var(--mut);border-bottom:2px solid transparent;font-weight:600}
  .tab.on{color:var(--txt);border-bottom-color:var(--cyan)}
  .panel{display:none} .panel.on{display:block}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px}
  h2{font-size:15px;margin:0 0 12px;letter-spacing:.3px} h3{font-size:13px;color:var(--mut);margin:0 0 8px;text-transform:uppercase;letter-spacing:.6px}
  .metric{display:grid;grid-template-columns:230px 1fr 96px;gap:12px;align-items:center;margin:8px 0}
  .bar{height:10px;border-radius:6px;background:#0c1428;overflow:hidden} .bar>span{display:block;height:100%}
  .mut{color:var(--mut)} .sev{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px}
  .sev-pass{background:#0f2e22;color:#6ee7b7}.sev-warn{background:#3a300f;color:#fcd34d}.sev-fail{background:#3a1414;color:#fca5a5}.sev-info{background:#16243f;color:#93c5fd}
  table{width:100%;border-collapse:collapse;font-size:12.5px} th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  th{color:var(--mut);font-weight:600;text-transform:uppercase;font-size:10.5px;letter-spacing:.5px}
  .turn{border:1px solid var(--line);border-radius:12px;margin-bottom:12px;overflow:hidden}
  .turn .top{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:#0e1730;padding:10px 14px;border-bottom:1px solid var(--line)}
  .turn .top .n{font-weight:800} .trap{font-family:Monaco,monospace;font-size:11px;color:var(--violet);background:#1b1735;border:1px solid #392c63;border-radius:6px;padding:2px 8px}
  .qa{padding:6px 14px 12px} .q{border-left:3px solid var(--violet);padding:6px 0 6px 12px;margin:8px 0;white-space:pre-wrap}
  .a{border-left:3px solid var(--cyan);padding:6px 0 6px 12px;margin:8px 0;white-space:pre-wrap}
  .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut)}
  .ok{color:var(--green);font-weight:700}.no{color:var(--red);font-weight:700}.na{color:#4b5d80}
  .grid-audit{overflow-x:auto} .grid-audit td.c{text-align:center;font-weight:700}
  details{border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:8px 0;background:var(--card2)}
  summary{cursor:pointer;font-weight:600} .persona{font-weight:700;color:var(--amber)}
  pre{white-space:pre-wrap;word-break:break-word;background:#0a1124;border:1px solid var(--line);border-radius:10px;padding:14px;font-size:11.5px;max-height:560px;overflow:auto}
  .empty{color:var(--mut);font-style:italic;padding:8px 0}
</style></head>
<body><div class="wrap">
  <div class="hdr">
    <div><div class="score" id="score"></div></div>
    <div id="cert"></div>
    <div style="flex:1"></div>
    <div id="chips" style="display:flex;flex-wrap:wrap;gap:8px"></div>
  </div>
  <div class="kpis" id="kpis"></div>
  <div id="exec"></div>
  <div class="tabs" id="tabs"></div>
  <div id="panels"></div>
</div>
<script id="report" type="application/json">__REPORT_JSON__</script>
<script>
const D = JSON.parse(document.getElementById('report').textContent);
const $ = (h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild;};
const esc = (s)=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num = (n)=> (typeof n==='number'? n.toLocaleString() : (n??'—'));

// ---- header ----
document.getElementById('score').innerHTML = `${(D.final_score??0).toFixed?D.final_score.toFixed(1):D.final_score} <small>/ 10</small>`;
const cert=(D.certification||'').toUpperCase();
const cls = cert==='GOLD'?'b-gold':cert==='SILVER'?'b-silver':'b-other';
document.getElementById('cert').innerHTML = `<span class="badge ${cls}">${esc(cert||'—')}</span>`;
const md=D.metadata||{};
const chips=[['mode',D.mode],['consensus',md.consensus_strategy],['turns',md.turns],['harness',md.model],['fallback',md.fallback_model]];
document.getElementById('chips').innerHTML = chips.filter(c=>c[1]!=null).map(c=>`<span class="pill">${c[0]}: <b style="color:var(--txt)">${esc(c[1])}</b></span>`).join('');

// ---- kpis (no cost) ----
const kpi=(v,l)=>`<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`;
document.getElementById('kpis').innerHTML =
  kpi(num(D.tokens_used),'tokens used') +
  kpi((D.duration_seconds??0)+'s','duration') +
  kpi(num((D.transcript||[]).length),'turns') +
  kpi(num(md.llm_call_count),'llm calls') +
  kpi(num((D.findings||[]).length),'findings') +
  kpi(Object.keys(D.per_metric||{}).length,'metrics');

// ---- exec brief ----
const execBits=[];
// "Graded against" — the assignment the jury judged the artifact against.
// A domain mismatch here (e.g. a refund BRD graded vs a 'library agent'
// role) is the #1 cause of an unexpectedly low score, so surface it first.
const _role=md.role, _bc=md.business_case, _goal=md.goal;
if(_role||_bc||_goal){
  let ga='<div style="margin-bottom:10px;padding:10px;border-radius:8px;background:rgba(120,120,160,.08)"><b style="font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut)">Graded against</b><br>';
  if(_role) ga+=`<div style="margin-top:4px"><b>Role:</b> ${esc(_role)}</div>`;
  if(_bc)   ga+=`<div><b>Business case:</b> ${esc(_bc)}</div>`;
  if(_goal) ga+=`<div><b>Goal:</b> ${esc(_goal)}</div>`;
  ga+='</div>'; execBits.push(ga);
}
if(D.executive_summary) execBits.push(`<p>${esc(D.executive_summary)}</p>`);
else if(D.summary) execBits.push(`<p>${esc(D.summary)}</p>`);
const pr=D.production_ready, tr=D.top_risk;
if(pr) execBits.push(`<span class="pill">production: <b style="color:var(--txt)">${esc(pr)}</b></span> `);
if(tr) execBits.push(`<span class="pill">top risk: <b style="color:var(--txt)">${esc(tr)}</b></span>`);
(D.warnings||[]).forEach(w=>execBits.push(`<div class="sev sev-warn" style="display:inline-block;margin-top:8px">⚠ ${esc(w)}</div>`));
if(execBits.length) document.getElementById('exec').innerHTML = `<div class="card"><h2>Executive brief</h2>${execBits.join('')}</div>`;

// ---- tabs ----
const TABS=[['overview','Overview'],['transcript','Transcript'],['audit','Turn audit'],['jury','Jury debate'],['findings','Findings'],['tech','Technical issues'],['raw','Raw JSON']];
const tabsEl=document.getElementById('tabs'), panelsEl=document.getElementById('panels');
TABS.forEach(([id,label],i)=>{
  tabsEl.appendChild($(`<div class="tab ${i===0?'on':''}" data-t="${id}">${label}</div>`));
  panelsEl.appendChild($(`<div class="panel ${i===0?'on':''}" id="p-${id}"></div>`));
});
tabsEl.addEventListener('click',e=>{const t=e.target.dataset.t; if(!t)return;
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x.dataset.t===t));
  document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('on',x.id==='p-'+t));});

// ---- overview: per-metric bars ----
const pm=D.per_metric||{}, conf=D.confidence||{}, sev=D.severity||{};
let ov=`<div class="card"><h2>Per-metric scores</h2>`;
const sevcls=s=>({pass:'sev-pass',warn:'sev-warn',fail:'sev-fail',info:'sev-info',critical:'sev-fail'}[String(s||'').toLowerCase()]||'sev-warn');
const col=v=> v>=8?'var(--green)': v>=5?'var(--amber)':'var(--red)';
Object.keys(pm).forEach(m=>{const v=pm[m];
  ov+=`<div class="metric"><div>${esc(m)}<div class="lbl">conf ${conf[m]??'—'}</div></div>
    <div class="bar"><span style="width:${(v/10*100)||0}%;background:${col(v)}"></span></div>
    <div><b>${v}</b>/10 <span class="sev ${sevcls(sev[m])}">${esc(sev[m]||'—')}</span></div></div>`;});
ov+=`</div>`;
document.getElementById('p-overview').innerHTML=ov;

// ---- transcript ----
const tr_=D.transcript||[];
let tx = tr_.length? '' : '<div class="empty">No transcript turns.</div>';
tr_.forEach(t=>{
  const def=(t.defects||[]).length;
  tx+=`<div class="turn"><div class="top"><span class="n">Turn ${t.turn_index}</span>
    ${t.trap_name?`<span class="trap">${esc(t.trap_name)}</span>`:''}
    ${def?`<span class="sev sev-fail">${def} defect(s)</span>`:`<span class="sev sev-pass">no defects</span>`}
    ${(t.tools_called||[]).length?`<span class="pill">${t.tools_called.length} tool call(s)</span>`:''}</div>
    <div class="qa"><div class="lbl">Harness</div><div class="q">${esc(t.question)}</div>
    <div class="lbl">Agent</div><div class="a">${esc(t.answer)}</div>
    ${t.reasoning?`<div class="lbl">Conductor reasoning</div><div class="mut">${esc(t.reasoning)}</div>`:''}</div></div>`;});
document.getElementById('p-transcript').innerHTML=tx;

// ---- turn audit matrix (turns x metrics, PASS/FAIL aggregated across jurors) ----
const cl=D.consensus_log||{};
const metrics=Object.keys(cl);
const turns=(D.transcript||[]).map(t=>t.turn_index);
function cell(metric,turn){
  const c=cl[metric]; if(!c) return null;
  // Use whichever round actually carries per_turn_audit rows (round_two is
  // the final verdict, but some runs only attach the audit to round_one).
  const r2has=(c.round_two||[]).some(j=>(j.per_turn_audit||[]).length);
  const jurors=(r2has?c.round_two:c.round_one)||[];
  let saw=false, fail=false;
  jurors.forEach(j=>(j.per_turn_audit||[]).forEach(a=>{ if(a.turn_index===turn){saw=true; if(String(a.outcome||'').toUpperCase().startsWith('F')) fail=true;}}));
  return !saw?null:(fail?'FAIL':'PASS');
}
let au;
if(!metrics.length||!turns.length){ au='<div class="empty">No per-turn audit data.</div>'; }
else{
  au=`<div class="card"><h2>Turn × metric audit <span class="mut">(aggregated across jurors; final round)</span></h2><div class="grid-audit"><table><thead><tr><th>turn</th>`;
  metrics.forEach(m=>au+=`<th>${esc(m.replace(/_/g,' '))}</th>`); au+=`</tr></thead><tbody>`;
  turns.forEach(t=>{ au+=`<tr><td><b>${t}</b></td>`;
    metrics.forEach(m=>{const r=cell(m,t);
      au+=`<td class="c">${r==='PASS'?'<span class="ok">✓</span>':r==='FAIL'?'<span class="no">✕</span>':'<span class="na">·</span>'}</td>`;});
    au+=`</tr>`;});
  au+=`</tbody></table></div></div>`;
}
document.getElementById('p-audit').innerHTML=au;

// ---- jury debate ----
let jr = metrics.length? '' : '<div class="empty">No consensus data.</div>';
metrics.forEach(m=>{const c=cl[m];
  jr+=`<div class="card"><h2>${esc(m)} — <b>${c.score}</b>/10 <span class="sev ${sevcls(c.severity)}">${esc(c.severity||'')}</span>
       <span class="mut" style="font-weight:400">conf ${c.confidence}, spread ${c.spread}, revote ${c.revote_triggered}</span></h2>`;
  [['round_one','Round 1'],['round_two','Round 2']].forEach(([rk,rl])=>{
    const votes=c[rk]||[]; if(!votes.length) return;
    jr+=`<h3>${rl}</h3>`;
    votes.forEach(v=>{ jr+=`<details><summary><span class="persona">${esc(v.persona)}</span> — score ${v.score}</summary>
      <div class="mut" style="margin-top:8px;white-space:pre-wrap">${esc(v.reasoning)}</div></details>`;});
  });
  jr+=`</div>`;});
document.getElementById('p-jury').innerHTML=jr;

// ---- findings ----
const fn=D.findings||[];
let fd= fn.length? `<table><thead><tr><th>severity</th><th>metric</th><th>headline</th><th>recommendation</th></tr></thead><tbody>` : '<div class="empty">No findings — the agent did not trigger any defects. </div>';
fn.forEach(f=>fd+=`<tr><td><span class="sev ${sevcls(f.severity)}">${esc(f.severity)}</span></td><td>${esc(f.metric)}</td><td><b>${esc(f.headline)}</b><div class="mut">${esc(f.detail)}</div></td><td class="mut">${esc(f.recommendation)}</td></tr>`);
if(fn.length) fd+=`</tbody></table>`;
document.getElementById('p-findings').innerHTML=`<div class="card"><h2>Findings (${fn.length})</h2>${fd}</div>`;

// ---- technical issues (operational/behavioral anomalies) ----
const ti=D.technical_issues||[];
let td= ti.length? `<table><thead><tr><th>severity</th><th>type</th><th>issue</th><th>fix</th></tr></thead><tbody>` : '<div class="empty">No technical issues — no agent refusals, tool-call defects, crashes, or harness errors during this run.</div>';
ti.forEach(f=>td+=`<tr><td><span class="sev ${sevcls(f.severity)}">${esc(f.severity)}</span></td><td class="mut">${esc(f.metric)}</td><td><b>${esc(f.headline)}</b><div class="mut">${esc(f.detail)}</div></td><td class="mut">${esc(f.recommendation)}</td></tr>`);
if(ti.length) td+=`</tbody></table>`;
document.getElementById('p-tech').innerHTML=`<div class="card"><h2>Technical issues (${ti.length})</h2><div class="mut" style="margin-bottom:8px">Operational &amp; behavioral anomalies seen during the run — separate from the agent-quality findings.</div>${td}</div>`;

// ---- raw ----
document.getElementById('p-raw').innerHTML=`<div class="card"><h2>Raw report JSON</h2><pre>${esc(JSON.stringify(D,null,2))}</pre></div>`;
</script>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="Path to a report .json saved by report.to_json().")
    ap.add_argument("--out", default=None, help="Output .html path (default: alongside the .json).")
    ap.add_argument("--open", action="store_true", help="Open the dashboard in your default browser.")
    args = ap.parse_args()

    src = Path(args.report).expanduser().resolve()
    if not src.exists():
        print(f"[error] report not found: {src}", file=sys.stderr)
        return 2
    data = json.loads(src.read_text())

    title = f"ProofAgent report — {src.stem}"
    # Embed the JSON safely (neutralize any literal </script> inside string values).
    blob = json.dumps(data).replace("</", "<\\/")
    html = _HTML.replace("__TITLE__", title).replace("__REPORT_JSON__", blob)

    out = Path(args.out).expanduser().resolve() if args.out else src.with_suffix(".html")
    out.write_text(html)
    print(f"Dashboard written: {out}")
    print(f"Open it:           open '{out}'")
    if args.open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
