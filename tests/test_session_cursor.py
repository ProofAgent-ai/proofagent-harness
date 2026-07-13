"""The Cursor capture adapter — discovery via workspace.json + prompt extraction."""

import json
import sqlite3
from pathlib import Path

from proofagent_harness.session.cursor import (
    cursor_session_key,
    discover_cursor_db,
    from_cursor,
)


def _make_db(storage: Path, repo: Path, prompts):
    d = storage / "abc123hash"
    d.mkdir()
    (d / "workspace.json").write_text(json.dumps({"folder": repo.as_uri()}), encoding="utf-8")
    db = d / "state.vscdb"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)",
                ("aiService.prompts", json.dumps(prompts)))
    con.execute("INSERT INTO ItemTable VALUES (?, ?)",
                ("composer.composerData", json.dumps(
                    {"allComposers": [{"filesChangedCount": 2, "totalLinesAdded": 10,
                                       "totalLinesRemoved": 3}]})))
    con.commit()
    con.close()
    return db


def test_discovers_db_by_workspace_folder(tmp_path, monkeypatch):
    storage, repo = tmp_path / "storage", tmp_path / "myrepo"
    storage.mkdir()
    repo.mkdir()
    monkeypatch.setenv("PROOFAGENT_CURSOR_STORAGE_DIR", str(storage))
    _make_db(storage, repo, [{"text": "Add a login page", "commandType": 4}])
    db = discover_cursor_db(str(repo))
    assert db is not None
    assert cursor_session_key(db) == "cursor:abc123hash"


def test_extracts_prompts_as_intents_skips_empty(tmp_path, monkeypatch):
    storage, repo = tmp_path / "storage", tmp_path / "myrepo"
    storage.mkdir()
    repo.mkdir()
    monkeypatch.setenv("PROOFAGENT_CURSOR_STORAGE_DIR", str(storage))
    db = _make_db(storage, repo, [
        {"text": "Add a login page", "commandType": 4},
        {"text": "   ", "commandType": 4},          # blank → skipped
        {"text": "Wire the payments API", "commandType": 4},
    ])
    events = from_cursor(db)  # no workspace → prompts only
    intents = [e for e in events if e.action == "prompt"]
    assert [e.content for e in intents] == ["Add a login page", "Wire the payments API"]
    start = next((e for e in events if e.kind == "session_start"), None)
    assert start and start.args["files_changed"] == 2 and start.args["tool_version"] == "cursor"


def test_missing_db_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFAGENT_CURSOR_STORAGE_DIR", str(tmp_path / "nope"))
    assert discover_cursor_db(str(tmp_path)) is None
