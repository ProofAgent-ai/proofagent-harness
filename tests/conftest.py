"""Shared pytest fixtures.

The tests do NOT call real LLMs by default. We inject a `FakeLLM` that returns
deterministic, schema-conformant JSON, so the test suite is fast, free, and
hermetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from proofagent_harness.llm import LLM, CompletionResult


@dataclass
class FakeLLM(LLM):
    """A deterministic stand-in for litellm-backed LLM. No network calls."""

    model: str = "fake/test"
    temperature: float = 0.0
    max_tokens: int = 256
    seed: int | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    # Sequence of canned replies for `complete`
    canned_text: list[str] = field(default_factory=list)
    # Sequence of canned dicts for `complete_json`
    canned_json: list[dict[str, Any]] = field(default_factory=list)

    # Capture the temperature passed to each call so tests can assert
    # juror calls used 0.0 etc.
    last_temperature_per_call: list[float | None] = field(default_factory=list)

    async def complete(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        self.last_temperature_per_call.append(temperature)
        text = self.canned_text.pop(0) if self.canned_text else "ok"
        self.call_count += 1
        self.total_tokens += 15
        return CompletionResult(text=text, prompt_tokens=10, completion_tokens=5, cost_usd=0.0)

    async def complete_json(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        self.last_temperature_per_call.append(temperature)
        # Mirror the real LLM's accounting so the harness's "did the LLM
        # actually run?" sanity check sees positive call_count + tokens.
        self.call_count += 1
        self.total_tokens += 15
        if self.canned_json:
            return self.canned_json.pop(0)
        # Default: a PROTOCOL-COMPLIANT juror score. The juror contract is
        # audit-first — a reply with no per_turn_audit is rejected as
        # unevaluated, so the stub must include one like a real juror.
        return {
            "score": 7.5,
            "reasoning": "deterministic stub score",
            "per_turn_audit": [
                {"turn_index": 1, "outcome": "PASS", "citation": "stub citation"}
            ],
        }


class _AlwaysErrorLLM(FakeLLM):
    """FakeLLM variant that simulates total auth failure — every call raises.

    Used to verify the harness's `LLMNotConfiguredError` safety net fires
    when juror calls all fail (the real-world cause: bad/missing API key).
    """

    async def complete(self, *args, **kwargs):  # type: ignore[override]
        from proofagent_harness.llm import LLMError

        raise LLMError("fake auth failure (test fixture)")

    async def complete_json(self, *args, **kwargs):  # type: ignore[override]
        from proofagent_harness.llm import LLMError

        raise LLMError("fake auth failure (test fixture)")


@pytest.fixture(autouse=True)
def _isolate_proofagent_home(tmp_path, monkeypatch):
    """Point PROOFAGENT_HOME at a per-test directory.

    Without this the suite reads and WRITES the developer's real
    ``~/.proofagent/transcripts``: any test that calls ``aevaluate`` persists a
    transcript there, and a later test with a matching fingerprint then replays it. That
    made a test outcome depend on whether an earlier test (or a real run) had left a
    cache behind — the context assessment was silently reused instead of being called,
    which is exactly the kind of order-dependent failure that is miserable to chase.
    Isolating it also stops the suite polluting the cache a user relies on.
    """
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path / "proofagent_home"))


@pytest.fixture
def always_error_llm() -> _AlwaysErrorLLM:
    return _AlwaysErrorLLM()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def echo_agent():
    """Trivial sync agent — echoes back a refusal-shaped message."""

    def agent(message: str) -> str:
        return f"I cannot help with that ({message[:40]}...). I'd recommend the official channel."

    return agent


@pytest.fixture
def stateful_agent():
    """Closure-based agent that tracks turn count."""
    history: list[dict[str, str]] = []

    def agent(message: str) -> str:
        history.append({"role": "user", "content": message})
        reply = (
            f"[turn {len(history)}] I'll need verification before I can do that."
        )
        history.append({"role": "assistant", "content": reply})
        return reply

    return agent
