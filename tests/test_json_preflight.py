"""Preflight: a harness LLM that cannot return structured JSON is refused up front.

A local model answered the jury ballot with bare strings and the run died at scoring,
after the whole conversation had been paid for. The preflight probe catches that before
turn 1, and names the fix (a stronger harness LLM)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from proofagent_harness import Harness
from proofagent_harness.harness import LLMNotConfiguredError

from .conftest import FakeLLM


class _BallotLLM(FakeLLM):
    """Answers the probe with a configurable payload."""

    model: str = "fake/ballot"

    def __init__(self, payload: Any = None) -> None:
        super().__init__()
        self.model = "fake/ballot"
        self.payload = payload

    async def complete_json(self, messages, *, schema=None, **kw):  # type: ignore[override]
        self.call_count += 1
        return self.payload


def _probe(payload: Any) -> None:
    llm = _BallotLLM(payload=payload)
    h = Harness(llm=llm, turns=2, verbose=False)
    asyncio.run(h._preflight_check_json(llm, "harness"))


def test_a_well_formed_ballot_passes() -> None:
    _probe({"check_votes": [
        {"check_id": "leaked_account_data", "turn_index": 1, "observed": False, "quote": ""},
        {"check_id": "refused_to_help", "turn_index": 1, "observed": False, "quote": ""},
    ]})


@pytest.mark.parametrize("payload", [
    {"check_votes": ["leaked_account_data", "refused_to_help"]},       # strings, not objects
    {"check_votes": "observed nothing"},                                 # not a list
    {"check_votes": []},                                                 # empty
    {"check_votes": [{"check_id": "x", "turn_index": "1", "observed": "no"}]},  # wrong types
    "just text",                                                         # not an object
])
def test_a_malformed_ballot_is_refused_with_the_fix_named(payload) -> None:
    with pytest.raises(LLMNotConfiguredError) as err:
        _probe(payload)
    msg = str(err.value)
    assert "cannot reliably return structured JSON" in msg
    assert "stronger harness LLM" in msg
    assert "fake/ballot" in msg


def test_the_probe_runs_in_preflight_and_can_be_skipped(monkeypatch) -> None:
    llm = _BallotLLM(payload={"check_votes": ["bad"]})
    h = Harness(llm=llm, turns=2, verbose=False)
    monkeypatch.delenv("PROOFAGENT_SKIP_JSON_PROBE", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        asyncio.run(h._preflight_check_llm())
    monkeypatch.setenv("PROOFAGENT_SKIP_JSON_PROBE", "1")
    asyncio.run(h._preflight_check_llm())   # explicit opt-out starts anyway
