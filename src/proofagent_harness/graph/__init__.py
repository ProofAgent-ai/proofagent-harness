"""LangGraph orchestration for the harness pipeline."""

from proofagent_harness.graph.state import HarnessState

# LAZY, to break a real cycle: `builder` imports the agent nodes, and an agent node importing
# `graph.state` would otherwise re-enter this package while `agents` was still initializing —
# agents -> graph -> builder -> agents. Eagerly importing `Harness` at the top of the package used
# to force a safe order and hide it; nothing should depend on that accident.
_LAZY = {"build_graph": "proofagent_harness.graph.builder"}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | set(__all__))


__all__ = ["HarnessState", "build_graph"]
