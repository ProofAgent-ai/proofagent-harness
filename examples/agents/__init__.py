"""Multi-agent JSON spec library + factory for the multi-agent benchmark.

Each .json file in this folder defines one application-agent with system
prompt, role/business_case/goal, tools (OpenAI function-tool schema), and
mock tool responses. The factory loads a spec and constructs a runtime
agent dispatched by LLM family (OpenAI, Anthropic, Gemini, xAI).
"""

from examples.agents.factory import (
    AgentSpec,
    load_agent_spec,
    make_agent_from_spec,
    make_context_from_spec,
)

__all__ = [
    "AgentSpec",
    "load_agent_spec",
    "make_agent_from_spec",
    "make_context_from_spec",
]
