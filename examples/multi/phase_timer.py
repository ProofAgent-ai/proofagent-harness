"""Run a command in a pseudo-terminal, tee its output to a log, and record when each harness phase starts.

  python phase_timer.py OUT.json LOG.txt -- <command ...>

The harness draws its progress ("[calibrate] … [plan] … [conduct] … [jury] …") only on a terminal, so the command
runs under a pty. OUT.json gets {"phases": [[name, seconds_from_start], ...], "total": s, "exit": code}."""
import json
import os
import pty
import re
import select
import subprocess
import sys
import time

MARKERS = [
    ("setup", re.compile(r"\[setup\]")),
    ("calibrate", re.compile(r"\[calibrat")),
    ("context", re.compile(r"\[context\]")),
    ("plan", re.compile(r"\[plan\]")),
    ("conduct", re.compile(r"\[conduct")),
    ("jury", re.compile(r"\[jury\]|\[jur")),
    ("report", re.compile(r"\[report|Certification:")),
    ("per_export", re.compile(r"PER 2\.1\.2 →")),
    ("upload", re.compile(r"Uploading PER")),
    ("done", re.compile(r"Release decision|Not uploaded")),
]


def main() -> int:
    out_json, log_path = sys.argv[1], sys.argv[2]
    cmd = sys.argv[sys.argv.index("--") + 1:]
    master, slave = pty.openpty()
    env = dict(os.environ, COLUMNS="200", LINES="50")
    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    seen: dict[str, float] = {}
    buf = ""
    with open(log_path, "w") as log:
        while True:
            r, _, _ = select.select([master], [], [], 1.0)
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                text = data.decode("utf-8", "replace")
                sys.stdout.write(text)
                sys.stdout.flush()
                log.write(text)
                buf = (buf + text)[-4000:]
                for name, rx in MARKERS:
                    if name not in seen and rx.search(buf):
                        seen[name] = round(time.time() - t0, 1)
            elif p.poll() is not None:
                break
    code = p.wait()
    phases = sorted(seen.items(), key=lambda kv: kv[1])
    json.dump({"phases": phases, "total": round(time.time() - t0, 1), "exit": code}, open(out_json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
