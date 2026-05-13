"""Shared state passed between LangGraph nodes."""

from __future__ import annotations

from collections.abc import Callable
from operator import add
from typing import Annotated, Any, TypedDict

from proofagent_harness.llm import LLM
from proofagent_harness.schemas import (
    AgentContext,
    ConsensusResult,
    EvaluationPlan,
    Event,
    Finding,
    JurorScore,
    Persona,
    Scoring,
    Skill,
    Trap,
    Turn,
)


def _merge_dicts(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Reducer: combine dicts, with b winning on key collision (per metric)."""
    out = dict(a)
    out.update(b)
    return out

def _extend_juror_scores(
    a: list[JurorScore], b: list[JurorScore]
) -> list[JurorScore]:
    """Reducer: append parallel juror scores into a flat list."""
    return [*a, *b]

class HarnessState(TypedDict, total=False):
    """The single source of truth for an in-flight evaluation."""

    role: str
    business_case: str
    goal: str
    turn_count: int
    metrics: list[str]
    knowledge_text: str
    context: AgentContext

    agent_callable: Callable[..., Any]

    llm: LLM

    skills: list[Skill]
    traps: list[Trap]
    trap_index: Any
    personas: list[Persona]

    plan: EvaluationPlan

    transcript: Annotated[list[Turn], add]
    current_turn: int

    round_one_scores: Annotated[list[JurorScore], _extend_juror_scores]
    round_two_scores: Annotated[list[JurorScore], _extend_juror_scores]
    metrics_to_revote: list[str]

    consensus: Annotated[dict[str, ConsensusResult], _merge_dicts]

    per_metric: dict[str, float]
    confidence: dict[str, float]
    final_score: float
    certification: str
    findings: list[Finding]
    warnings: list[str]
    summary: str

    consensus_strategy: str
    debate_rounds: int
    revote_threshold: float
    scoring_config: Scoring
    on_event: Callable[[Event], None] | None
    cost_usd: float
    tokens_used: int

    context_budget_chars: int
