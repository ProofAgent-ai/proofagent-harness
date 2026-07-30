"""Invalid JSON escapes are repaired instead of burning a fallback.

A juror asked to quote text verbatim copies a backslash straight into a JSON string, and a
backslash that does not begin a valid escape is fatal to the parser. Measured on a real
run: 6 of the first ~36 juror calls died this way. Each one fell back to a larger, pricier
model — so part of the scoring was done by a DIFFERENT model than the rest, which quietly
undermines run-to-run comparability. Repairing at the parser is cheaper and keeps one
scorer for the whole run.
"""

from __future__ import annotations

import json

import pytest

from proofagent_harness.llm import _parse_json_loose, _repair_escapes

# Every escape JSON actually allows. None of these may be touched by the repair.
VALID_ESCAPES = ('\\"', "\\\\", "\\/", "\\b", "\\f", "\\n", "\\r", "\\t", "\\u00e9")


@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    [
        # The exact production failure: a backslash followed by a non-escape character,
        # which the parser reports as an invalid \uXXXX escape.
        ("invalid u escape", '{"quote": "see \\update the file"}', "see \\update the file"),
        ("windows path", '{"quote": "C:\\users\\alice"}', "C:\\users\\alice"),
        ("invalid x escape", '{"quote": "\\x41 literal"}', "\\x41 literal"),
        ("short unicode", '{"quote": "\\u12 truncated"}', "\\u12 truncated"),
        ("several in one string", '{"quote": "\\a \\c \\q"}', "\\a \\c \\q"),
    ],
)
def test_invalid_escapes_are_repaired(label: str, raw: str, expected: str) -> None:
    assert _parse_json_loose(raw)["quote"] == expected, label


def test_every_valid_escape_is_left_alone() -> None:
    """A correctly escaped payload must round-trip byte-for-byte."""
    for esc in VALID_ESCAPES:
        payload = '{"a": "x' + esc + 'y"}'
        assert _repair_escapes(payload) == payload, f"repair modified {esc!r}"
        _parse_json_loose(payload)                     # and it still parses


def test_a_mixed_payload_keeps_the_valid_and_fixes_the_invalid() -> None:
    raw = '{"a": "newline\\nhere", "b": "raw \\path"}'
    got = _parse_json_loose(raw)
    assert got["a"] == "newline\nhere"                 # real newline, preserved
    assert got["b"] == "raw \\path"                    # literal backslash, repaired


def test_fences_and_prose_still_work() -> None:
    """The pre-existing tolerances must not regress."""
    assert _parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_loose('Here you go: {"a": 2} hope that helps') == {"a": 2}


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": ',                          # truncated
        '{"a": 1,}',                       # trailing comma
        # Unterminated: the \" is a VALID escape, so the string never closes. Ambiguous,
        # and repair must not guess.
        '{"quote": "ends with \\"}',
        "not json at all",
    ],
)
def test_genuinely_broken_json_still_raises(raw: str) -> None:
    """Repair must not paper over a truncated or malformed reply — that would turn a
    failed call into a silently wrong result, which is worse than a fallback."""
    with pytest.raises((json.JSONDecodeError, ValueError)):
        _parse_json_loose(raw)


def test_the_juror_prompt_asks_for_json_safe_quotes() -> None:
    """The prompt caused this: it said "copy the exact characters, do not clean them up"."""
    from proofagent_harness.agents.juror import _CHECK_PROTOCOL

    assert "backslash" in _CHECK_PROTOCOL.lower()
    assert "do not clean them up" not in _CHECK_PROTOCOL


def test_the_vote_schema_caps_the_quote_length() -> None:
    """Uncapped, a juror pasted a whole tool payload as one "quote", ran past the output
    budget, and truncated the JSON mid-string — deterministically, so every retry failed
    identically and the call fell through to a different model. That split one run's
    scoring across two models. Prose asking for brevity was not enough; the schema has
    to say it."""
    import asyncio
    from typing import Any

    from proofagent_harness.agents.juror import _vote_once

    seen: dict[str, Any] = {}

    class _Spy:
        model = "spy"

        async def complete_json(self, messages, *, schema=None, **kw):
            seen["schema"] = schema
            return {"check_votes": []}

    asyncio.run(_vote_once(
        _Spy(), {}, type("P", (), {"name": "p"})(), "safety",
        [{"check_id": "refused_clearly", "turn_index": 1, "ask": "?",
          "polarity": "positive"}],
        "sys", "user", round_num=1, debate_round=0,
    ))
    quote = seen["schema"]["properties"]["check_votes"]["items"]["properties"]["quote"]
    assert quote.get("maxLength") == 300, quote


def test_the_prompt_states_the_quote_limit_too() -> None:
    """The schema enforces it; the prompt has to tell the juror so it does not try."""
    from proofagent_harness.agents.juror import _CHECK_PROTOCOL

    assert "300" in _CHECK_PROTOCOL

