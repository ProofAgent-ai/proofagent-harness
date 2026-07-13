"""The durable local session accumulator (proof watch's on-disk source of truth)."""

from proofagent_harness.session.events import SessionEvent
from proofagent_harness.session.live import LiveSession
from proofagent_harness.session.runner import SessionRunner


def _fake_secret(prefix: str, n: int = 44) -> str:
    """Synthetic, format-valid credential built at call time from digits, so no
    token-shaped literal is committed while the screen still flags it (not a
    placeholder)."""
    return prefix + "".join(str((i * 7 + 3) % 10) for i in range(n))


def _result():
    events = [
        SessionEvent(seq=-1, kind="session_start", action="other",
                     args={"agent_output_tokens": 500, "agent_input_tokens": 1500}),
        SessionEvent(seq=0, kind="message", action="prompt", content="Add a Stripe checkout"),
        SessionEvent(seq=1, kind="tool", tool="edit", action="write", target="billing.py",
                     content=f'stripe.api_key = "{_fake_secret("sk_live_")}"',
                     tokens=500),
        SessionEvent(seq=2, kind="message", action="prompt", content="Run the tests"),
        SessionEvent(seq=3, kind="tool", tool="bash", action="exec", target="pytest -q", tokens=0),
    ]
    return SessionRunner().run(events, capabilities={"tool": "claude-code"})


def test_accumulates_tokens_intents_and_flagged(tmp_path):
    s = LiveSession.load_or_new("sess-abc", workspace="/w", tool="claude-code",
                                state_dir=str(tmp_path))
    s.refresh(_result(), stamp="2026-07-10T00:00:00Z")
    assert s.global_tokens["total"] == 2000  # counted regardless of findings
    assert len(s.intents) == 2               # two real prompts
    assert s.flagged_count == 1              # only the turn that leaked a secret
    risk = s.flagged[0]["risks"][0]
    assert risk["severity"] == "critical"
    assert "…" in risk["proof"]              # proof is redacted, not the raw key


def test_persists_and_reloads(tmp_path):
    s = LiveSession.load_or_new("k", state_dir=str(tmp_path))
    s.refresh(_result(), stamp="t1")
    p = s.save(state_dir=str(tmp_path))
    assert p.exists()
    s2 = LiveSession.load_or_new("k", state_dir=str(tmp_path))
    assert s2.global_tokens["total"] == 2000
    assert len(s2.intents) == 2
    # scans counter accumulates across ticks (survives reload)
    s2.refresh(_result(), stamp="t2")
    assert s2.scans == 2


def test_corrupt_state_file_does_not_crash(tmp_path):
    p = LiveSession.path_for("k", state_dir=str(tmp_path))
    p.write_text("{ not valid json", encoding="utf-8")
    s = LiveSession.load_or_new("k", state_dir=str(tmp_path))  # must not raise
    assert s.session_key == "k" and s.scans == 0
