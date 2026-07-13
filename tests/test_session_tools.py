"""The coding-tool registry — friendly names (agent attribution) + discovery method."""

from proofagent_harness.session.tools import display_name, resolve


def test_friendly_agent_names():
    assert display_name("claude-code") == "Claude Code"
    assert display_name("cursor") == "Cursor"
    assert display_name("copilot") == "GitHub Copilot"


def test_discovery_method_per_tool():
    assert resolve("claude-code").discovery == "transcript"  # readable JSONL log
    assert resolve("cursor").discovery == "cursor-db"        # SQLite state.vscdb
    assert resolve("windsurf").discovery == "git"            # working-tree diff


def test_unknown_tool_is_titlecased_and_git():
    t = resolve("my-new-tool")
    assert t.name == "My New Tool"
    assert t.discovery == "git"
