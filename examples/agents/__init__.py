"""Multi-agent JSON spec library + factory.

Each ``.json`` file in this folder defines one production-style domain agent
with system prompt, role / business_case / goal, knowledge corpus, tool
catalog (OpenAI function-tool schema), and stub tool responses. The factory
loads a spec and constructs a runtime agent callable dispatched by LLM
family (OpenAI, Anthropic, Gemini, xAI).

Reusable agent specs shared by the examples (e.g. ``08_live_trace.py``).
"""

from .factory import (
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
