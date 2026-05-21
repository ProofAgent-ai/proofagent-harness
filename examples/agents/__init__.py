"""Multi-agent JSON spec library + factory.

Each ``.json`` file in this folder defines one production-style domain agent
with system prompt, role / business_case / goal, knowledge corpus, tool
catalog (OpenAI function-tool schema), and stub tool responses. The factory
loads a spec and constructs a runtime agent callable dispatched by LLM
family (OpenAI, Anthropic, Gemini, xAI).

Used by ``examples/09_asymmetric_single_cell.py`` to reproduce the
asymmetric evaluation cells reported in the paper. Reusable as a starting
point for your own multi-agent benchmarks.
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
