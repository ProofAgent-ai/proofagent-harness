"""Pipeline agents — each one is a LangGraph node.

LAZY, because importing this package used to import EVERY node, and the nodes reach the whole
orchestration stack — the conductor pulls the context budgeter, which pulls litellm. So anything
wanting one pure helper from here (the audit layer wants `consensus.credit_for` to recompute credit
while reading a report) paid for the entire pipeline, and could not run at all without it installed.

PEP 562: `from proofagent_harness.agents import conductor_node` behaves exactly as before.
"""

from __future__ import annotations

_LAZY: dict[str, str] = {
    "compliance_assessor_node": "proofagent_harness.agents.compliance_assessor",
    "conductor_node": "proofagent_harness.agents.conductor",
    "consensus_node": "proofagent_harness.agents.consensus",
    "context_assessor_node": "proofagent_harness.agents.context_assessor",
    "jury_round_one_node": "proofagent_harness.agents.juror",
    "jury_round_two_node": "proofagent_harness.agents.juror",
    "planner_node": "proofagent_harness.agents.planner",
    "reporter_node": "proofagent_harness.agents.reporter",
    "should_continue_conducting": "proofagent_harness.agents.conductor",
    "should_revote": "proofagent_harness.agents.consensus",
    "verify_actions": "proofagent_harness.agents.policy_verifier",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = sorted(_LAZY)
