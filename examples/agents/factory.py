"""Agent factory — build a runtime agent from a JSON spec.

A spec is a single JSON file with fields:
    name, role, domain, business_case, goal, system_prompt, knowledge?,
    tools[], skills[], guardrails[]

Each tool has: name, description, parameters (OpenAI function-tool schema),
and an optional `stub` field defining the mock response when the tool is
invoked. Stubs let the harness evaluate the agent's BEHAVIOR (does it call
verify_identity before issue_refund?) without needing real backends.

Provider dispatch by model name:
    claude-* / anthropic/* -> Anthropic SDK
    gemini/*               -> LiteLLM (Gemini provider)
    xai/* or grok-*        -> LiteLLM (xAI provider)
    everything else        -> OpenAI SDK (or any OpenAI-compatible proxy)

The factory returns (agent_callable, AgentContext) so the caller can pass
both to Harness.evaluate(...).
"""
from __future__ import annotations

import importlib.util
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofagent_harness.schemas import AgentContext, AgentResponse

# Reuse model-family detection + retry helpers from 01_quickstart so we get
# the GPT-5.x deprecated-param caching, Anthropic SDK retry, Gemini routing,
# etc., without duplicating the code.
_QS_PATH = Path(__file__).resolve().parent.parent / "01_quickstart.py"
_spec = importlib.util.spec_from_file_location("quickstart", _QS_PATH)
assert _spec and _spec.loader, f"01_quickstart.py not found at {_QS_PATH}"
_qs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_qs)

_is_anthropic_model = _qs._is_anthropic_model
_is_gemini_model = _qs._is_gemini_model
_get_openai_client = _qs._get_openai_client
_chat_create_with_retry = _qs._chat_create_with_retry
_anthropic_messages_create_with_retry = _qs._anthropic_messages_create_with_retry


def _is_xai_model(model: str) -> bool:
    m = model.lower()
    return m.startswith("xai/") or m.startswith("grok-")


@dataclass
class AgentSpec:
    name: str
    role: str
    domain: str
    business_case: str
    goal: str
    system_prompt: str
    knowledge: str
    skills: list[str]
    guardrails: list[str]
    tools_openai: list[dict[str, Any]]
    tools_anthropic: list[dict[str, Any]]
    tool_stubs: dict[str, Any]
    raw: dict[str, Any]


def load_agent_spec(path: str | Path) -> AgentSpec:
    """Load + validate a JSON agent spec."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"agent spec not found: {p}")
    data = json.loads(p.read_text())
    required = ("name", "role", "system_prompt", "tools", "business_case", "goal")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"agent spec {p.name} missing required fields: {missing}")

    tools_openai = []
    tools_anthropic = []
    stubs: dict[str, Any] = {}
    for t in data["tools"]:
        tname = t["name"]
        tdesc = t.get("description", "")
        tparams = t.get("parameters", {"type": "object", "properties": {}, "required": []})
        tools_openai.append({
            "type": "function",
            "function": {"name": tname, "description": tdesc, "parameters": tparams},
        })
        tools_anthropic.append({
            "name": tname,
            "description": tdesc,
            "input_schema": tparams,
        })
        stubs[tname] = t.get("stub", {"status": "stub_ok"})

    return AgentSpec(
        name=data["name"],
        role=data["role"],
        domain=data.get("domain", "generic"),
        business_case=data["business_case"],
        goal=data["goal"],
        system_prompt=data["system_prompt"],
        knowledge=data.get("knowledge", ""),
        skills=data.get("skills", []),
        guardrails=data.get("guardrails", []),
        tools_openai=tools_openai,
        tools_anthropic=tools_anthropic,
        tool_stubs=stubs,
        raw=data,
    )


def make_context_from_spec(spec: AgentSpec) -> AgentContext:
    """Build the AgentContext that the harness passes to jurors for reference."""
    return AgentContext(
        system_prompt=spec.system_prompt,
        tools=spec.tools_openai,
        knowledge=spec.knowledge,
    )


def make_agent_from_spec(spec: AgentSpec, model: str) -> Callable[[str], AgentResponse]:
    """Construct an agent callable, dispatched by model family."""
    if _is_anthropic_model(model):
        return _make_anthropic_agent_inline(model, spec)
    if _is_gemini_model(model) or _is_xai_model(model):
        return _make_litellm_agent_inline(model, spec)
    return _make_openai_agent_inline(model, spec)


def _execute_stub(name: str, args: dict[str, Any], stubs: dict[str, Any]) -> Any:
    """Return the mock response for a tool name. Args are accepted but ignored
    for the stub — the harness scores BEHAVIOR (was the tool called with the
    right shape?), not the realism of the response."""
    if name not in stubs:
        return {"error": f"unknown tool: {name}"}
    return stubs[name]


def _make_openai_agent_inline(model: str, spec: AgentSpec) -> Callable[[str], AgentResponse]:
    """OpenAI-flavored agent — chat.completions API with function tools.

    Same invariant as Anthropic: every assistant message with tool_calls must
    be followed by tool messages for each call_id before any next user turn."""
    history: list[dict[str, Any]] = [{"role": "system", "content": spec.system_prompt}]

    def agent(message: str) -> AgentResponse:
        history.append({"role": "user", "content": message})
        tools_called: list[dict[str, Any]] = []
        final_text = ""

        for _ in range(5):  # bounded tool roundtrips
            r = _chat_create_with_retry(
                model=model,
                messages=history,
                tools=spec.tools_openai,
                temperature=0,
                max_tokens=1024,
            )
            choice = r.choices[0]
            msg = choice.message

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            history.append(assistant_msg)

            # No tool_calls → natural text-only turn, exit.
            if not tool_calls:
                final_text = (msg.content or "").strip()
                break

            # Tool_calls present → ALWAYS append matching tool messages so the
            # pattern is closed before any next user message.
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = _execute_stub(tc.function.name, args, spec.tool_stubs)
                tools_called.append(
                    {"name": tc.function.name, "args": args, "result": result}
                )
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

            # If finish_reason wasn't tool_calls, model intended this as final
            # response — don't loop again.
            if choice.finish_reason != "tool_calls":
                final_text = (msg.content or "").strip()
                break

        return AgentResponse(text=final_text, tools_called=tools_called)

    return agent


def _make_anthropic_agent_inline(model: str, spec: AgentSpec) -> Callable[[str], AgentResponse]:
    """Anthropic-flavored agent — messages API with native tool_use blocks.

    Critical invariant: every assistant message containing tool_use blocks
    MUST be followed by a user message with matching tool_result blocks,
    otherwise Anthropic rejects all subsequent turns with
    'tool_use ids were found without tool_result blocks immediately after'.
    We enforce this by ALWAYS executing tool_uses if present, regardless of
    stop_reason — then deciding loop continuation based on stop_reason."""
    history: list[dict[str, Any]] = []

    def agent(message: str) -> AgentResponse:
        history.append({"role": "user", "content": message})
        tools_called: list[dict[str, Any]] = []
        final_text = ""

        for _ in range(5):
            r = _anthropic_messages_create_with_retry(
                model=model,
                max_tokens=1024,
                temperature=0,
                system=spec.system_prompt,
                tools=spec.tools_anthropic,
                messages=history,
            )

            text_chunks: list[str] = []
            tool_uses: list[Any] = []
            for block in r.content:
                if block.type == "text":
                    text_chunks.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            history.append({"role": "assistant", "content": r.content})

            # If response has NO tool_use blocks, this is a natural text-only
            # turn — just exit.
            if not tool_uses:
                final_text = "\n".join(c for c in text_chunks if c).strip()
                break

            # Tool_use blocks are present — ALWAYS append matching tool_result
            # blocks so the pattern is closed before any next user message.
            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                args = dict(tu.input)
                result = _execute_stub(tu.name, args, spec.tool_stubs)
                tools_called.append({"name": tu.name, "args": args, "result": result})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                })
            history.append({"role": "user", "content": tool_results})

            # If stop_reason wasn't tool_use, the model intended this as a
            # final response (max_tokens, end_turn). Don't loop again — return
            # the partial text it produced.
            if r.stop_reason != "tool_use":
                final_text = "\n".join(c for c in text_chunks if c).strip()
                break

        return AgentResponse(text=final_text, tools_called=tools_called)

    return agent


def _make_litellm_agent_inline(model: str, spec: AgentSpec) -> Callable[[str], AgentResponse]:
    """LiteLLM-routed agent — Gemini, xAI, and other providers. Uses OpenAI
    function-tool format; LiteLLM translates to each provider's native shape."""
    import litellm

    history: list[dict[str, Any]] = [{"role": "system", "content": spec.system_prompt}]

    def agent(message: str) -> AgentResponse:
        history.append({"role": "user", "content": message})
        tools_called: list[dict[str, Any]] = []
        final_text = ""

        for _ in range(5):
            r = _litellm_completion_with_retry(
                model=model,
                messages=history,
                tools=spec.tools_openai,
                temperature=0,
                max_tokens=1024,
            )
            choice = r.choices[0]
            msg = choice.message

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            history.append(assistant_msg)

            if not tool_calls:
                final_text = (msg.content or "").strip()
                break

            # Tool_calls present → ALWAYS append matching tool messages
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = _execute_stub(tc.function.name, args, spec.tool_stubs)
                tools_called.append(
                    {"name": tc.function.name, "args": args, "result": result}
                )
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

            if choice.finish_reason != "tool_calls":
                final_text = (msg.content or "").strip()
                break
        _ = litellm  # keep import live for type/usage
        return AgentResponse(text=final_text, tools_called=tools_called)

    return agent


def _litellm_completion_with_retry(**kwargs: Any) -> Any:
    """LiteLLM call with exponential backoff and deprecated-param handling
    (drops temperature / renames max_tokens for reasoning models that reject)."""
    import litellm

    max_retries = 4
    base_delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            mutated = False
            if (
                "max_tokens" in msg
                and "max_completion_tokens" in msg
                and "max_tokens" in kwargs
                and "max_completion_tokens" not in kwargs
            ):
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                mutated = True
            if (
                "temperature" in msg
                and any(s in msg for s in ("does not support", "unsupported", "deprecated"))
                and "temperature" in kwargs
            ):
                kwargs.pop("temperature", None)
                mutated = True
            if mutated:
                continue
            last_exc = exc
            transient_substrings = (
                "rate limit", "503", "502", "504", "overloaded",
                "connection", "timeout",
            )
            if attempt < max_retries and any(s in msg for s in transient_substrings):
                wait = base_delay * (2 ** attempt)
                time.sleep(wait)
                continue
            raise
    if last_exc is not None:
        raise last_exc
