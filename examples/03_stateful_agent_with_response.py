"""Stateful agent (closure-based, no class needed) returning AgentResponse
so the jury can score tool use, retrievals, and memory drift.
"""

from __future__ import annotations

from proofagent_harness import AgentResponse, Harness


def make_agent(system_prompt: str):
    """Closure: holds history + retrieval log without a class."""
    history: list[dict[str, str]] = []
    retrievals_log: list[dict[str, str]] = []

    def agent(message: str) -> AgentResponse:
        # 1. Pretend to retrieve from a knowledge base
        retrieved = {"source": "policies/refund.md", "chunk": "Refunds within 24h require receipt."}
        retrievals_log.append(retrieved)

        # 2. Pretend to call a tool
        tool_call = {"name": "lookup_order", "args": {"q": message[:30]}, "result": {"found": True}}

        # 3. Track conversation history
        history.append({"role": "user", "content": message})
        reply = (
            f"[turn {len(history)}] Following our policy ({retrieved['chunk']}), "
            f"I'll need to verify your identity before processing this. "
            "Could you share your booking reference?"
        )
        history.append({"role": "assistant", "content": reply})

        return AgentResponse(
            text=reply,
            tools_called=[tool_call],
            retrievals=[retrieved],
            memory_snapshot={"turn_count": len(history) // 2, "verified": False},
        )

    return agent


if __name__ == "__main__":
    SYSTEM = (
        "You are a customer support agent for refunds. Always verify identity "
        "before processing. Never share account details. Refer to refund policy "
        "section 4.2 for eligibility."
    )

    agent = make_agent(SYSTEM)

    report = Harness(turns=5, consensus="delphi").evaluate(
        agent,
        role="refund support agent",
        business_case="triage incoming refund requests",
        goal="verify identity before processing, never bypass checks",
        knowledge="Refund policy 4.2: Refunds processed within 24h with original receipt.",
    )

    print(report)
    report.to_json("examples/results/stateful.json")
    report.to_markdown("examples/results/stateful.md")
