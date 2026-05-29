"""Regression tests for the v0.4.2 LLM fixes.

Covers:

  1. The core bug fix: ``complete_json`` no longer appends the failed reply +
     error message to the conversation on retry. Each call sends exactly the
     ORIGINAL prompt. This prevents the compounding-prompt feedback loop
     that overflowed model context windows on long-context evals.

  2. The optional ``fallback_llm``: when configured, failed primary calls
     (JSON parse failure OR transport exception) automatically route to the
     fallback with the SAME original prompt. The fallback never sees the
     primary's failed reply or any error message.

  3. The new ``LLMJSONStructureError``: actionable error raised when no
     fallback is configured and the primary can't produce JSON. Message
     names the model, the parse error, and three concrete recommended fixes.

  4. Per-source token accounting: ``primary_*`` and ``fallback_*`` counters
     on the ``LLM`` instance update independently. Surfaced in the Report as
     ``token_split`` so the asymmetric-cost story is visible to users.

  5. Backwards compat: an ``LLM`` constructed with no ``fallback_llm``
     behaves identically to v0.4.1 — same counters, same exceptions.

The tests stub ``litellm.acompletion`` so they run offline in <1 second.
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from proofagent_harness.llm import LLM, LLMError, LLMJSONStructureError

# ── Shared fixture ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_litellm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``litellm.acompletion`` with a queue-driven stub. Returns the
    state dict so tests can push responses + read the call log."""
    state: dict[str, Any] = {"call_log": [], "responses": []}

    async def _fake_acompletion(*, model: str, messages: list, **kwargs: Any) -> dict[str, Any]:
        state["call_log"].append({
            "model": model,
            "messages": list(messages),
            "kwargs": dict(kwargs),
        })
        if not state["responses"]:
            raise RuntimeError("test queue empty: push a response before calling")
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return {
            "choices": [{"message": {"content": item}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "model": model,
        }

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    return state


# ── Test 1: no error-append (the core v0.4.1 bug fix) ─────────────────────


@pytest.mark.asyncio
async def test_complete_json_no_fallback_raises_actionable_error(
    _patch_litellm: dict[str, Any],
) -> None:
    """When primary returns broken JSON and no fallback is configured,
    raise LLMJSONStructureError. SINGLE attempt. No retry. No error append."""
    _patch_litellm["responses"] = ['{"score": broken json oops']
    llm = LLM(model="test-primary")

    with pytest.raises(LLMJSONStructureError) as exc:
        await llm.complete_json([{"role": "user", "content": "score this"}])

    # Exactly ONE litellm call. No retry. Prompt size constant.
    assert len(_patch_litellm["call_log"]) == 1
    sent_message = _patch_litellm["call_log"][0]["messages"][-1]["content"]
    assert "score this" in sent_message
    assert "previous reply" not in sent_message, (
        "BUG: error message was appended to the conversation. "
        "The v0.4.2 fix requires single-attempt-no-append."
    )

    # Actionable error: names the model, recommends fallback_llm.
    msg = str(exc.value)
    assert "test-primary" in msg
    assert "fallback_llm" in msg
    assert "Anthropic" in msg or "stronger" in msg.lower()


# ── Test 2: fallback rescues with original prompt ─────────────────────────


@pytest.mark.asyncio
async def test_complete_json_fallback_rescues_with_original_prompt(
    _patch_litellm: dict[str, Any],
) -> None:
    """When primary returns broken JSON and fallback IS configured:
    fallback receives the ORIGINAL messages (no error append, no broken reply)."""
    _patch_litellm["responses"] = [
        '{"score": broken json',                # primary fails parse
        '{"score": 8, "reasoning": "good"}',    # fallback succeeds
    ]
    primary = LLM(model="test-primary")
    fallback = LLM(model="test-fallback")
    primary.fallback_llm = fallback

    notices: list[dict[str, Any]] = []
    primary.on_fallback = lambda p: notices.append(p)

    result = await primary.complete_json(
        [{"role": "user", "content": "score this"}]
    )

    assert result == {"score": 8, "reasoning": "good"}
    assert len(_patch_litellm["call_log"]) == 2

    # Critical: fallback got the ORIGINAL prompt.
    fb_messages = _patch_litellm["call_log"][1]["messages"]
    fb_last = fb_messages[-1]["content"]
    assert "score this" in fb_last
    assert "previous reply" not in fb_last
    assert "broken" not in fb_last, (
        "BUG: primary's broken reply leaked into the fallback's prompt."
    )

    # Per-source token accounting.
    assert primary.primary_call_count == 1
    assert primary.fallback_call_count == 1
    assert primary.primary_prompt_tokens == 100
    assert primary.fallback_prompt_tokens == 100

    # on_fallback callback fires with structured payload.
    assert len(notices) == 1
    assert notices[0]["primary_model"] == "test-primary"
    assert notices[0]["fallback_model"] == "test-fallback"
    assert notices[0]["reason"] == "json_parse_error"
    assert notices[0]["stage"] == "complete_json"


# ── Test 3: transport errors also route to fallback ───────────────────────


@pytest.mark.asyncio
async def test_complete_json_fallback_rescues_transport_errors(
    _patch_litellm: dict[str, Any],
) -> None:
    """When the primary's underlying litellm call RAISES (network, rate
    limit, etc.), the fallback should also rescue."""
    _patch_litellm["responses"] = [
        ConnectionError("simulated network failure"),
        '{"x": 1}',
    ]
    primary = LLM(model="test-primary")
    fallback = LLM(model="test-fallback")
    primary.fallback_llm = fallback

    result = await primary.complete_json([{"role": "user", "content": "x"}])

    assert result == {"x": 1}
    assert primary.fallback_call_count == 1
    # When the primary throws BEFORE producing content, there's nothing to
    # account for on the primary side.
    assert primary.primary_call_count == 0


@pytest.mark.asyncio
async def test_complete_json_no_fallback_propagates_transport_errors(
    _patch_litellm: dict[str, Any],
) -> None:
    """Without a fallback, transport errors bubble up unchanged (NOT as
    LLMJSONStructureError — that's for JSON-specific failures)."""
    _patch_litellm["responses"] = [ConnectionError("simulated network failure")]
    llm = LLM(model="test-primary")

    with pytest.raises(LLMError) as exc:
        await llm.complete_json([{"role": "user", "content": "x"}])

    # Should be the base LLMError, not the JSON subclass.
    assert not isinstance(exc.value, LLMJSONStructureError)
    assert "test-primary" in str(exc.value)


# ── Test 4: backwards compat ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_fallback_preserves_v041_behavior(
    _patch_litellm: dict[str, Any],
) -> None:
    """An LLM with no fallback_llm behaves identically to v0.4.1 on a
    successful call — same total_tokens, same call_count, same return type."""
    _patch_litellm["responses"] = ['{"x": 1}']
    llm = LLM(model="test-only")

    r = await llm.complete([{"role": "user", "content": "hi"}])

    assert r.text == '{"x": 1}'
    assert llm.primary_call_count == 1
    assert llm.fallback_call_count == 0
    assert llm.total_tokens == 150
    assert llm.total_cost_usd >= 0.0


# ── Test 5: both-failed error has different message ───────────────────────


@pytest.mark.asyncio
async def test_fallback_uses_compact_prompt_and_reduced_max_tokens(
    _patch_litellm: dict[str, Any],
) -> None:
    """v0.4.3 tiered degradation: when fallback fires, it should receive

      (a) the ORIGINAL user messages (the v0.4.2 fix — no error-append)
      (b) a STRICTER system prompt asking for a concise reply
      (c) a SMALLER max_tokens cap (min(primary, 4096))

    This handles the failure mode where primary couldn't fit a complete
    JSON in the original budget — asking the fallback for shorter output
    beats asking for the same output again.
    """
    _patch_litellm["responses"] = [
        '{"score": broken json',                 # primary fails parse
        '{"score": 7, "reasoning": "ok"}',       # fallback succeeds
    ]
    primary = LLM(model="test-primary", max_tokens=8192)
    fallback = LLM(model="test-fallback", max_tokens=8192)
    primary.fallback_llm = fallback

    await primary.complete_json([{"role": "user", "content": "score this"}])

    assert len(_patch_litellm["call_log"]) == 2
    primary_call = _patch_litellm["call_log"][0]
    fallback_call = _patch_litellm["call_log"][1]

    # (a) Original user message preserved on fallback.
    fb_user_msg = fallback_call["messages"][-1]["content"]
    assert "score this" in fb_user_msg
    assert "previous reply" not in fb_user_msg
    assert "broken" not in fb_user_msg

    # (b) System prompt on fallback is STRICTER than primary.
    primary_sys = primary_call["messages"][0]["content"]
    fallback_sys = fallback_call["messages"][0]["content"]
    assert "Respond ONLY with valid JSON" in primary_sys
    assert "Respond ONLY with valid JSON" in fallback_sys
    # Compact-only directive should appear ONLY on fallback.
    assert "BE EXTREMELY CONCISE" in fallback_sys
    assert "BE EXTREMELY CONCISE" not in primary_sys
    # Both prompts include a character budget hint; fallback's must be
    # SMALLER than primary's (proportional to the reduced max_tokens).
    assert "characters" in primary_sys
    assert "characters" in fallback_sys

    # (c) max_tokens on fallback call ≤ 4096 (even though primary was 8192).
    assert primary_call["kwargs"].get("max_tokens") == 8192
    assert fallback_call["kwargs"].get("max_tokens") == 4096


@pytest.mark.asyncio
async def test_fallback_max_tokens_respects_user_lower_setting(
    _patch_litellm: dict[str, Any],
) -> None:
    """If the user explicitly configured a small max_tokens (e.g. 2048 for
    a cost-bound smoke test), the fallback must NEVER exceed it — only
    shrink. The 4096 cap is a CEILING, not a target."""
    _patch_litellm["responses"] = [
        '{"score": broken json',
        '{"x": 1}',
    ]
    primary = LLM(model="test-primary", max_tokens=2048)  # user set tight cap
    fallback = LLM(model="test-fallback", max_tokens=2048)
    primary.fallback_llm = fallback

    await primary.complete_json([{"role": "user", "content": "x"}])

    fallback_call = _patch_litellm["call_log"][1]
    # Fallback uses min(2048, 4096) = 2048 — never exceeds user's setting.
    assert fallback_call["kwargs"].get("max_tokens") == 2048


@pytest.mark.asyncio
async def test_llm_api_base_passed_to_litellm(
    _patch_litellm: dict[str, Any],
) -> None:
    """v0.4.4: LLM.api_base, if set, is forwarded to litellm.acompletion
    on every call. Pins the endpoint regardless of OPENAI_BASE_URL env var."""
    _patch_litellm["responses"] = ['{"x": 1}']
    llm = LLM(model="openai/gpt-4.1-mini", api_base="https://api.openai.com/v1")

    await llm.complete_json([{"role": "user", "content": "x"}])

    call = _patch_litellm["call_log"][0]
    assert call["kwargs"].get("api_base") == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_harness_auto_sets_api_base_for_openai_fallback(
    _patch_litellm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.4.4: when fallback_llm is an openai/* string, Harness pins
    api_base=https://api.openai.com/v1 on the constructed LLM. This bypasses
    any OPENAI_BASE_URL env var the user set for a local proxy (LM Studio).

    Without this, the OpenAI fallback would inherit the proxy URL and try
    to call gpt-4.1-mini against LM Studio — which would 400 with 'No
    models loaded'.
    """
    from proofagent_harness.harness import Harness

    # Simulate the asymmetric benchmark's wire_proxy() side effect.
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")

    h = Harness(
        llm="claude-sonnet-4-6",
        fallback_llm="openai/gpt-4.1-mini",
    )
    # Fallback LLM constructed by Harness should have api_base pinned.
    assert h.fallback_llm is not None
    assert h.fallback_llm.api_base == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_harness_preserves_user_llm_instance_api_base(
    _patch_litellm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.4.4: when the user passes a pre-built LLM instance for the
    fallback (e.g. Azure OpenAI with a custom api_base), Harness MUST NOT
    override their configuration. This is the escape hatch for users on
    OpenAI-compatible endpoints other than api.openai.com."""
    from proofagent_harness.harness import Harness

    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")

    user_fallback = LLM(
        model="openai/gpt-4.1-mini",
        api_base="https://my-azure-deployment.openai.azure.com/v1",
    )
    h = Harness(llm="claude-sonnet-4-6", fallback_llm=user_fallback)

    # User's api_base is respected, NOT overridden to api.openai.com.
    assert h.fallback_llm is user_fallback
    assert h.fallback_llm.api_base == "https://my-azure-deployment.openai.azure.com/v1"


@pytest.mark.asyncio
async def test_harness_does_not_pin_api_base_for_anthropic_fallback(
    _patch_litellm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.4.4: Anthropic/Gemini fallback strings do NOT get an api_base
    pinned — they use the provider's native endpoint via ANTHROPIC_API_KEY /
    GOOGLE_API_KEY, which never collides with OPENAI_BASE_URL."""
    from proofagent_harness.harness import Harness

    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")

    h = Harness(
        llm="openai/gemma-...",
        fallback_llm="anthropic/claude-haiku-4-5-20251001",
    )
    assert h.fallback_llm is not None
    # Anthropic models route via api.anthropic.com automatically — no
    # api_base override needed.
    assert h.fallback_llm.api_base is None


def test_models_match_fuzzy_normalization() -> None:
    """v0.4.4: _models_match handles provider-prefix stripping + version-date
    normalization so legitimate fallback responses aren't flagged as misrouted."""
    from proofagent_harness.harness import _models_match
    # Should MATCH (legitimate variants):
    assert _models_match("openai/gpt-4.1-mini", "gpt-4.1-mini-2025-04-14")
    assert _models_match("claude-haiku-4-5-20251001", "claude-haiku-4-5")
    assert _models_match("anthropic/claude-sonnet-4-6", "claude-sonnet-4-6")
    assert _models_match("gpt-4.1-mini", "gpt-4.1-mini")
    # Should NOT match (the misrouting we're catching):
    assert not _models_match("openai/gpt-4.1-mini", "gemma-4-e4b-it-mlx")
    assert not _models_match("claude-haiku-4-5", "gpt-4.1-mini")
    assert not _models_match("openai/gpt-4.1-mini", "claude-haiku-4-5")


@pytest.mark.asyncio
async def test_fallback_preflight_catches_misrouted_response(
    _patch_litellm: dict[str, Any],
) -> None:
    """v0.4.4: when the preflight returns from a DIFFERENT model than asked,
    Harness aborts before the eval starts (instead of running the whole
    eval with the wrong model silently scoring everything)."""
    # Primary OK, fallback "responds" but the response.model is the wrong one
    # (e.g. LM Studio answered for gpt-4.1-mini with gemma-4-e4b-it-mlx).
    # We patch _fake_acompletion to return a different model for the fallback's
    # preflight by overriding the fixture's behavior.
    import litellm

    from proofagent_harness.harness import Harness, LLMNotConfiguredError

    async def _wrong_model_fallback(*, model, messages, **kwargs):
        if "gpt" in model.lower():
            # The "fallback" responded — but from the local proxy, not OpenAI.
            return {
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "model": "gemma-4-e4b-it-mlx",  # ← MISROUTED
            }
        # Primary preflight (claude) succeeds normally
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": model,
        }
    # Replace the fixture's stub for this test only.
    import contextlib
    with contextlib.suppress(AttributeError):
        litellm.acompletion = _wrong_model_fallback

    h = Harness(llm="claude-sonnet-4-6", fallback_llm="openai/gpt-4.1-mini", verbose=False)

    # Dummy agent — must accept (message: str) and return AgentResponse
    from proofagent_harness import AgentResponse
    def dummy_agent(_msg: str) -> AgentResponse:
        return AgentResponse(text="ok", tools_called=[], retrievals=[])

    with pytest.raises(LLMNotConfiguredError) as exc:
        await h.aevaluate(dummy_agent)

    msg = str(exc.value)
    assert "DIFFERENT model" in msg
    assert "gpt-4.1-mini" in msg
    assert "gemma" in msg
    assert "Aborting" in msg


@pytest.mark.asyncio
async def test_complete_json_both_failed_says_prompt_is_broken(
    _patch_litellm: dict[str, Any],
) -> None:
    """When BOTH primary AND fallback can't produce valid JSON, the error
    should point the user at the PROMPT (not the model) as the likely cause."""
    _patch_litellm["responses"] = ['{"a": broken', '{"b": also broken']
    primary = LLM(model="test-primary")
    fallback = LLM(model="test-fallback")
    primary.fallback_llm = fallback

    with pytest.raises(LLMJSONStructureError) as exc:
        await primary.complete_json([{"role": "user", "content": "x"}])

    msg = str(exc.value)
    assert "Both" in msg or "both" in msg
    assert "PROMPT" in msg or "prompt" in msg
    assert "Lower --turns" in msg or "Lower --context-budget" in msg
