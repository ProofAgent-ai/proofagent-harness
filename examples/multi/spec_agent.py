"""Agent-under-test wrapper for a production-style spec in ../agents (SPEC env var)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import factory  # noqa: E402

_SPEC = factory.load_agent_spec(Path(__file__).resolve().parents[1] / "agents" / os.environ["SPEC"])
_MODEL = os.environ.get("AGENT_LLM", "gpt-5-nano")


def _make():
    # Local models (LM Studio) go through LiteLLM; the OpenAI-SDK path cannot reach them.
    if _MODEL.startswith(("lm_studio/", "ollama/", "hosted_vllm/")):
        return factory._make_litellm_agent_inline(_MODEL, _SPEC)
    return factory.make_agent_from_spec(_SPEC, _MODEL)


agent = _make()


def reset():
    global agent
    agent = _make()
