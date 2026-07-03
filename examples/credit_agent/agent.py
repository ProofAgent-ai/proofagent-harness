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
    text = ""

    # Small agentic loop: let the model call tools, feed a result back for EACH tool_call
    # (the OpenAI API requires it), and continue until it produces a text reply. Capped so
    # it can never loop forever.
    for _ in range(4):
        resp = litellm.completion(
            model=AGENT_LLM,
            messages=[{"role": "system", "content": _SYSTEM}, *_history],
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
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

    if _HAS_AR:
        return AgentResponse(
            text=text or "(the agent issued tool calls without a text reply)",
            tools_called=tools_called,
            retrievals=[{"source": "domain_knowledge/credit_policy.md"}],
        )
    # Fallback if AgentResponse isn't importable: plain string still evaluates.
    return text or "(tool calls issued)"


# Alias so `proof run agent.py` finds it whichever name it looks for.
my_agent = agent


if __name__ == "__main__":
    # Manual smoke test: python examples/credit_agent/agent.py
    reset()
    print(agent("Hi, I'd like to check the status of my credit card application APP-10293."))
