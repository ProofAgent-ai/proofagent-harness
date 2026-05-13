"""Build the LangGraph StateGraph that wires all 5 agents together."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from proofagent_harness.agents import (
    consensus_node,
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
    """Compile and return the harness StateGraph."""
    g: StateGraph = StateGraph(HarnessState)

    g.add_node("planner", planner_node)
    g.add_node("conductor", conductor_node)
    g.add_node("jury_round_one", jury_round_one_node)
    g.add_node("consensus_check", consensus_node)
    g.add_node("jury_round_two", jury_round_two_node)
    g.add_node("finalize_consensus", finalize_consensus_node)
    g.add_node("reporter", reporter_node)

    g.add_edge(START, "planner")
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
    g.add_edge("reporter", END)

    return g.compile()
