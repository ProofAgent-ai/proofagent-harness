"""
credit-allocation agent UNDER TEST.

This is the agent ProofAgent evaluates. It is a real LLM agent: it loads its own
system prompt + tool schemas from ./context, keeps conversation history so it behaves
coherently across a multi-turn adversarial conversation, and answers each message.

The Harness imports this file and calls `agent(message)` once per turn.

Entry points (the CLI/harness picks up `agent`; `my_agent` is an alias for compatibility):
    agent(message: str) -> AgentResponse
    reset()  -> clear conversation history between evaluations

Config (env):
    AGENT_LLM   the model the agent itself runs on (default: gpt-4.1-mini)
                e.g. gpt-4.1, anthropic/claude-haiku-4-5, ollama/llama3.1
Requires the provider key for AGENT_LLM (e.g. OPENAI_API_KEY / ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import litellm

try:
    # Preferred: return the rich response so Tool Use / retrieval scoring is deepest.
    from proofagent_harness import AgentResponse  # type: ignore
    _HAS_AR = True
except Exception:  # pragma: no cover - lets the file import even without the harness
    _HAS_AR = False

HERE = Path(__file__).parent
CONTEXT = HERE / "context"

AGENT_LLM = os.getenv("AGENT_LLM", "gpt-4.1-mini")
# Fully-qualified model id for cost attribution: a bare OpenAI name (gpt-4.1-mini)
# is prefixed so the governance dashboard prices it as `measured`, not `estimated`.
_MODEL_ID = AGENT_LLM if "/" in AGENT_LLM else f"openai/{AGENT_LLM}"

SYSTEM_PROMPT = (CONTEXT / "system_prompt.md").read_text()
TOOLS = json.loads((CONTEXT / "tools.json").read_text())
# Ground the agent in its domain knowledge so Hallucination Resistance is testable.
# domain_knowledge/ is a SEPARATE folder (sibling of context/), matching the
# --domain-knowledge-dir CLI arg.
_KB_DIR = HERE / "domain_knowledge"
KNOWLEDGE = "\n\n".join(
    f"### {p.name}\n{p.read_text()}" for p in sorted(_KB_DIR.glob("*.md"))
)

_SYSTEM = (
    SYSTEM_PROMPT
    + "\n\n---\n# Reference knowledge (follow exactly)\n"
    + KNOWLEDGE
)

# Conversation history persists across turns within one evaluation.
_history: list[dict] = []

# Some newer models (e.g. Anthropic Opus 4.7+ / Fable 5) REJECT `temperature`.
# Track once whether this AGENT_LLM accepts it so we don't re-hit the error
# every turn — the same drop-and-retry pattern the harness LLM wrapper uses.
_AGENT_ACCEPTS_TEMPERATURE = True


def _system_message() -> dict:
    """The agent's system message. `_SYSTEM` (system prompt + full knowledge
    corpus, ~4k+ tokens) is IDENTICAL on every tool-loop call and every turn, so
    for Anthropic models we mark it `cache_control: ephemeral` — the provider
    caches it and bills cached reads at ~10% on all subsequent calls (big saving
    on a multi-turn agentic loop). OpenAI-compatible providers cache long
    prefixes automatically, so a plain string is already optimal there."""
    m = AGENT_LLM.lower()
    if "claude" in m or "anthropic" in m:
        return {"role": "system",
                "content": [{"type": "text", "text": _SYSTEM,
                             "cache_control": {"type": "ephemeral"}}]}
    return {"role": "system", "content": _SYSTEM}


def _agent_completion(**kwargs):
    """litellm.completion for the agent under test, resilient to a model that
    deprecated `temperature` (drops it and retries, then remembers)."""
    global _AGENT_ACCEPTS_TEMPERATURE
    if not _AGENT_ACCEPTS_TEMPERATURE:
        kwargs.pop("temperature", None)
    try:
        return litellm.completion(**kwargs)
    except litellm.BadRequestError as exc:
        if "temperature" in str(exc).lower() and "temperature" in kwargs:
            _AGENT_ACCEPTS_TEMPERATURE = False
            kwargs.pop("temperature", None)
            return litellm.completion(**kwargs)
        raise


def reset() -> None:
    """Clear conversation state (call between evaluations)."""
    _history.clear()


def _stub_tool_result(name: str, args: dict) -> str:
    """Demo tool executor — returns generic, plausible results so the agent can finish
    its turn. Replace with your real tool implementations. (These are stubs, not real data.)"""
    stubs = {
        "verify_identity": '{"verified": true, "last4_ssn": "1234"}',
        "get_credit_bureau_report": '{"fico": 705, "utilization": 0.38, "delinquencies_24mo": 1, "bureau": "experian"}',
        "check_fraud_signals": '{"fraud": false, "flags": []}',
        "calculate_dti": '{"dti": 0.41}',
        "allocate_credit_limit": '{"status": "approved"}',
        "send_adverse_action_notice": '{"status": "sent"}',
        "flag_for_human_review": '{"status": "escalated"}',
    }
    return stubs.get(name, '{"ok": true}')


def agent(message: str):
    """Answer one turn from the conductor. Returns AgentResponse (or str fallback)."""
    _history.append({"role": "user", "content": message})
    tools_called: list[dict] = []
    usage: list[dict] = []  # per-call token usage for THIS turn (performance metering)
    text = ""

    # Small agentic loop: let the model call tools, feed a result back for EACH tool_call
    # (the OpenAI API requires it), and continue until it produces a text reply. Capped so
    # it can never loop forever.
    for _ in range(4):
        resp = _agent_completion(
            model=AGENT_LLM,
            messages=[_system_message(), *_history],
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
        # Capture this internal call's token usage — the provider counts the full
        # request (system prompt + tools + knowledge + history), so this is exact.
        u = getattr(resp, "usage", None)
        if u is not None:
            det = getattr(u, "prompt_tokens_details", None)
            usage.append({
                "model": _MODEL_ID,
                "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                "cache_read_tokens": (getattr(det, "cached_tokens", 0) or 0) if det else 0,
            })
        choice = resp.choices[0].message
        text = choice.content or ""
        calls = getattr(choice, "tool_calls", None) or []

        # Record the assistant turn (with its tool_calls, if any).
        _history.append({
            "role": "assistant",
            "content": text,
            **({"tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in calls
            ]} if calls else {}),
        })

        if not calls:
            break  # final text reply — this turn is done

        # Respond to EACH tool_call with a result message so the next call's history is valid.
        for tc in calls:
            try:
                cargs = json.loads(tc.function.arguments or "{}")
            except Exception:
                cargs = {"_raw": tc.function.arguments}
            tools_called.append({"name": tc.function.name, "arguments": cargs})
            _history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _stub_tool_result(tc.function.name, cargs),
            })

    # A more agentic model (e.g. Fable 5 / Opus) may still be calling tools when the
    # loop cap is hit, leaving no text reply — an empty turn the jury (correctly)
    # scores as non-execution. Give it ONE final chance to summarize WITHOUT tools,
    # so the turn ends with a real user-facing answer regardless of how many tool
    # rounds the model used.
    if not text:
        final = _agent_completion(
            model=AGENT_LLM,
            messages=[_system_message(), *_history,
                      {"role": "user", "content": "Now give the applicant your final answer in plain text."}],
            tool_choice="none",
            temperature=0.2,
        )
        text = (final.choices[0].message.content or "").strip()
        fu = getattr(final, "usage", None)
        if fu is not None:
            usage.append({"model": _MODEL_ID,
                          "input_tokens": getattr(fu, "prompt_tokens", 0) or 0,
                          "output_tokens": getattr(fu, "completion_tokens", 0) or 0,
                          "cache_read_tokens": 0})

    if _HAS_AR:
        response: object = AgentResponse(
            text=text or "(the agent issued tool calls without a text reply)",
            tools_called=tools_called,
            retrievals=[{"source": "domain_knowledge/credit_policy.md"}],
        )
    else:
        # Fallback if AgentResponse isn't importable: plain string still evaluates.
        response = text or "(tool calls issued)"

    # (answer, usage) performance contract — usage is the per-call token list for THIS
    # turn. The harness prices it locally via litellm and surfaces latency/tokens/cost on
    # the dashboard. Drop the `, usage` to opt out (latency/turns still populate).
    return response, usage


# Alias so `proof run agent.py` finds it whichever name it looks for.
my_agent = agent


if __name__ == "__main__":
    # Manual smoke test: python examples/credit_agent/agent.py
    reset()
    print(agent("Hi, I'd like to check the status of my credit card application APP-10293."))
