"""Tier-2 escalation gating + auto-discovery - the cost-control contract.

These tests never hit an LLM: they lock down WHEN Tier-2 is allowed to spend
(only on criticals, by default) and WHAT slice it would send, plus the
no-file/no-flag auto-discovery of a Claude Code session.
"""

from __future__ import annotations

import json

from proofagent_harness.session.adapters import (
    _claude_transcript_for,
    discover_session,
    load_session,
)
from proofagent_harness.session.escalate import (
    assess_critical_slice,
    build_critical_slice,
    should_escalate,
)
from proofagent_harness.session.events import SessionEvent
from proofagent_harness.session.screen import ScreenContext, screen_events


def _mk(seq, **kw):
    return SessionEvent(seq=seq, **kw)


# ── Gating: Tier-2 must not fire without a critical ──────────────────────────

def test_no_escalation_when_clean():
    events = [_mk(0, tool="read", action="read", target="src/app.py")]
    findings = screen_events(events, ScreenContext(), None)
    assert should_escalate(findings, on="critical") is False
    out = assess_critical_slice(events, findings, mode="auto")
    assert out.triggered is False
    assert out.status == "not_triggered"
    assert out.token_usage["total_tokens"] == 0


def test_never_mode_is_always_zero_cost():
    events = [_mk(0, tool="write", action="write", target="a.py", content="sk-" + "A" * 24)]
    findings = screen_events(events, ScreenContext(), None)
    assert any(f.severity == "critical" for f in findings)  # a real critical exists
    out = assess_critical_slice(events, findings, mode="never")
    assert out.triggered is False and out.status == "disabled"  # forced off anyway


def test_escalate_on_high_lowers_the_bar():
    # A lone TLS-verify-disabled write is HIGH, not critical.
    events = [_mk(0, tool="write", action="write", target="net.py", content="requests.get(u, verify=False)")]
    findings = screen_events(events, ScreenContext(), None)
    sev = {f.severity for f in findings}
    assert "high" in sev and "critical" not in sev
    assert should_escalate(findings, on="critical") is False
    assert should_escalate(findings, on="high") is True


# ── Slice: only critical, code-bearing events; commands are context only ─────

def test_slice_aggregates_only_critical_code():
    events = [
        _mk(0, tool="write", action="write", target="src/pay.py",
            content="API_KEY = 'sk-" + "Z" * 24 + "'"),          # critical secret
        _mk(1, tool="read", action="read", target="src/util.py",
            content="print('hello')"),                            # not flagged
        _mk(2, tool="bash", action="exec", target="rm -rf /"),  # critical cmd, no code
    ]
    findings = screen_events(events, ScreenContext(), None)
    blob, seqs = build_critical_slice(events, findings, on="critical")
    assert seqs == [0]                       # only the code write
    assert "src/pay.py" in blob
    assert "src/util.py" not in blob         # clean event excluded
    assert "rm -rf /" in blob                # command appended as context
    assert "# Critical code slice" in blob


def test_slice_empty_when_criticals_have_no_code():
    events = [_mk(0, tool="bash", action="exec", target="rm -rf /")]
    findings = screen_events(events, ScreenContext(), None)
    assert should_escalate(findings, on="critical") is True   # rm -rf / is critical
    blob, seqs = build_critical_slice(events, findings, on="critical")
    assert blob == "" and seqs == []
    # ...so the LLM is never called - status is 'no_code', still zero tokens.
    out = assess_critical_slice(events, findings, mode="auto")
    assert out.triggered is True and out.status == "no_code"
    assert out.token_usage["total_tokens"] == 0


def test_slice_is_bounded():
    big = "x = 'sk-" + "Q" * 24 + "'\n" + ("pad\n" * 50_000)
    events = [_mk(0, tool="write", action="write", target="big.py", content=big)]
    findings = screen_events(events, ScreenContext(), None)
    blob, _ = build_critical_slice(events, findings, on="critical", max_chars=4_000)
    assert len(blob) <= 4_000


# ── Auto-discovery: no file, no flag ─────────────────────────────────────────

def test_discover_claude_transcript(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    ws.mkdir()
    import re
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(ws.resolve()))
    projects = tmp_path / "claude_projects"
    (projects / slug).mkdir(parents=True)
    transcript = projects / slug / "sess.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "x.py", "content": "y=1"}}
        ]},
    }) + "\n")
    monkeypatch.setenv("PROOFAGENT_CLAUDE_PROJECTS_DIR", str(projects))

    found = _claude_transcript_for(str(ws))
    assert found == transcript

    events, tool = discover_session(tool="auto", workspace=str(ws))
    assert tool == "claude-code"
    assert any(e.tool == "write" for e in events)


def test_load_session_prefers_explicit_source(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(json.dumps({"tool": "write", "target": "z.py", "content": "z=1"}) + "\n")
    events, tool = load_session(source=str(f), tool="cursor", workspace=str(tmp_path), from_git_flag=False)
    assert tool == "cursor"
    assert len(events) == 1 and events[0].target == "z.py"


# ── Scored path: Tier-2 report merges into the session payload ────────────────

class _FakeEnum:
    def __init__(self, value):
        self.value = value


class _FakeFinding:
    def __init__(self):
        self.metric = "secure_coding"
        self.severity = _FakeEnum("high")
        self.headline = "Hard-coded credential"
        self.detail = "The key is committed in source."
        self.recommendation = "Move it to a secret store."


class _FakeReport:
    def __init__(self):
        self.final_score = 4.2
        self.certification = _FakeEnum("NEEDS_ENHANCEMENT")
        self.per_metric = {"secure_coding": 3.0, "correctness": 6.0}
        self.executive_summary = "Critical slice has a hard-coded secret."
        self.top_risk = "Hard-coded credential"
        self.findings = [_FakeFinding()]


class _FakeHarness:
    def __init__(self, *a, **k):
        pass

    def evaluate(self, *a, **k):
        return _FakeReport()


def test_scored_path_merges_into_payload(monkeypatch):
    """When Tier-2 escalates AND an LLM is available, its score/findings/cost
    ride into the session payload while the Tier-1 gate is untouched."""
    import proofagent_harness
    from proofagent_harness.session import SessionRunner, build_session_payload

    monkeypatch.setattr(proofagent_harness, "Harness", _FakeHarness, raising=False)

    events = [_mk(0, tool="write", action="write", target="pay.py",
                  content="API_KEY = 'sk-" + "Z" * 24 + "'")]
    result = SessionRunner().run(events)
    assert any(f.severity == "critical" for f in result.findings)

    out = assess_critical_slice(events, result.findings, mode="auto")
    assert out.status == "scored"
    assert out.final_score == 4.2
    assert out.certification == "NEEDS_ENHANCEMENT"
    assert out.dimensions["secure_coding"] == 3.0
    assert len(out.findings) == 1
    assert out.findings[0]["finding_type"] == "quality_issue"  # never a block rule

    result.tier2 = {**out.as_dict(), "findings_payload": out.findings}
    payload = build_session_payload(result, agent_name="cursor session")
    # Tier-2 finding appended to the Tier-1 findings; metadata carries tier2.
    assert any(f["finding_type"] == "quality_issue" for f in payload["findings"])
    assert payload["run_metadata"]["tier2"]["status"] == "scored"
    assert "findings_payload" not in payload["run_metadata"]["tier2"]  # stripped
    assert "Tier-2 escalated" in payload["executive_summary"]
