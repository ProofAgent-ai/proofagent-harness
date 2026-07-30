"""Build the LangGraph StateGraph that wires all 5 agents together."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from proofagent_harness.agents import (
    compliance_assessor_node,
    consensus_node,
    context_assessor_node,
    jury_round_one_node,
    jury_round_two_node,
    planner_node,
    reporter_node,
    should_continue_conducting,
    should_revote,
)
from proofagent_harness.agents.conductor import conductor_node
from proofagent_harness.agents.consensus import finalize_consensus_node
from proofagent_harness.graph.state import HarnessState


def build_graph():
    """Compile and return the harness StateGraph for MULTI-TURN mode.

    Pipeline: context assessor → planner → conductor (loop) → jury R1 →
    consensus → optional jury R2 → finalize → reporter → compliance.
    """
    g: StateGraph = StateGraph(HarnessState)

    # Context is assessed FIRST so it can steer trap selection and weight the
    # behavioural score. It used to run inside the reporter, where the Q axis could
    # only be reported beside E, never inform it. No-op unless --assess-context.
    g.add_node("context_assessor", context_assessor_node)
    g.add_node("planner", planner_node)
    g.add_node("conductor", conductor_node)
    g.add_node("jury_round_one", jury_round_one_node)
    g.add_node("consensus_check", consensus_node)
    g.add_node("jury_round_two", jury_round_two_node)
    g.add_node("finalize_consensus", finalize_consensus_node)
    g.add_node("reporter", reporter_node)
    # Compliance runs AFTER the reporter so it can reuse the jury's enriched
    # findings (synthesized Problem/Proof/Fix). No-op unless --assess-compliance.
    g.add_node("compliance_assessor", compliance_assessor_node)

    g.add_edge(START, "context_assessor")
    g.add_edge("context_assessor", "planner")
    g.add_edge("planner", "conductor")

    g.add_conditional_edges(
        "conductor",
        should_continue_conducting,
        {"next": "conductor", "done": "jury_round_one"},
    )

    g.add_edge("jury_round_one", "consensus_check")

    g.add_conditional_edges(
        "consensus_check",
        should_revote,
        {"revote": "jury_round_two", "skip": "finalize_consensus"},
    )

    g.add_edge("jury_round_two", "finalize_consensus")
    g.add_edge("finalize_consensus", "reporter")
    g.add_edge("reporter", "compliance_assessor")
    g.add_edge("compliance_assessor", END)

    return g.compile()


def build_artifact_graph():
    """Compile and return the SLIM StateGraph for ARTIFACT mode.

    Pipeline: jury R1 → consensus → optional jury R2 → finalize → reporter.
    No planner, no conductor — the caller pre-populates state['transcript']
    with the synthetic 1-turn Turn built from the artifact + business_case.

    The same jury / consensus / reporter nodes are reused unchanged. Only
    difference vs. the multi-turn graph: no planner / conductor nodes, no
    conditional loop for the conductor.
    """
    g: StateGraph = StateGraph(HarnessState)

    g.add_node("jury_round_one", jury_round_one_node)
    g.add_node("consensus_check", consensus_node)
    g.add_node("jury_round_two", jury_round_two_node)
    g.add_node("finalize_consensus", finalize_consensus_node)
    g.add_node("reporter", reporter_node)
    g.add_node("compliance_assessor", compliance_assessor_node)

    g.add_edge(START, "jury_round_one")
    g.add_edge("jury_round_one", "consensus_check")

    g.add_conditional_edges(
        "consensus_check",
        should_revote,
        {"revote": "jury_round_two", "skip": "finalize_consensus"},
    )

    g.add_edge("jury_round_two", "finalize_consensus")
    g.add_edge("finalize_consensus", "reporter")
    g.add_edge("reporter", "compliance_assessor")
    g.add_edge("compliance_assessor", END)

    return g.compile()
