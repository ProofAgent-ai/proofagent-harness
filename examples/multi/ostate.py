"""Update results/overnight/state.json: python ostate.py log "text" | phase N status [note] | headline "text" | run agent/rep"""
import json, sys, time
from pathlib import Path
p = Path("results/overnight/state.json"); s = json.loads(p.read_text())
cmd = sys.argv[1]
if cmd == "log":
    s["log"].append([time.strftime("%H:%M"), sys.argv[2]])
elif cmd == "phase":
    ph = s["phases"][int(sys.argv[2])]; ph["status"] = sys.argv[3]
    if len(sys.argv) > 4: ph.setdefault("notes", []).append(sys.argv[4])
elif cmd == "headline":
    s["headline"] = sys.argv[2]
elif cmd == "run":
    s["current_run"] = sys.argv[2]
elif cmd == "json":
    s.update(json.loads(sys.argv[2]))
p.write_text(json.dumps(s, indent=1))
