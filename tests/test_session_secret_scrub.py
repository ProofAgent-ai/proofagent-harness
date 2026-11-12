"""Nothing that looks like a key leaves the machine in a watch/session upload (measured: a pa_live_ key typed into a
terminal-tool command was uploaded raw in run_metadata.access_map.mcp_tools)."""
from proofagent_harness.session.runner import _scrub_payload
from proofagent_harness.session.screen import _SECRET_PATTERNS

KEY = "pa_live_" + "d" * 48
NANO = "pa_nano_" + "a1" * 20


def test_platform_keys_are_a_recognised_secret():
    assert any(p.search(KEY) for _, p in _SECRET_PATTERNS)
    assert any(p.search(NANO) for _, p in _SECRET_PATTERNS)


def test_every_string_in_the_payload_is_scrubbed():
    payload = {"run_metadata": {"access_map": {"mcp_tools": [f"run_in_terminal UPLOAD_KEY='{KEY}' ./run.sh"]}},
               "events": [{"message": f"export PROOFAGENT_API_KEY={NANO}"}], "score": 9.0}
    out = _scrub_payload(payload)
    flat = str(out)
    assert KEY not in flat and NANO not in flat
    assert out["score"] == 9.0 and "run_in_terminal UPLOAD_KEY=" in out["run_metadata"]["access_map"]["mcp_tools"][0]
