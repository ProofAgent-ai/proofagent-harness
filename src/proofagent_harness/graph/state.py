"""Shared state passed between LangGraph nodes.

All nodes read from and write to this single TypedDict. LangGraph reducers
(`Annotated[..., ...]`) handle parallel writes from the jury fan-out.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, Callable, TypedDict

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
    """The single source of truth for an in-flight evaluation.

    All nodes read from / write to this dict. Reducers handle parallel writes.
    """

    # ── inputs (set once at graph entry) ──────────────────────────────────
    role: str
    business_case: str
    goal: str
    turn_count: int
    metrics: list[str]
    knowledge_text: str
    context: AgentContext

    # the user's agent — opaque callable, passed through state
    agent_callable: Callable[..., Any]

    # the BYO LLM the agents call — passed through state so we don't need globals
    llm: LLM

    # bundled or user-supplied
    skills: list[Skill]
    traps: list[Trap]
    trap_index: Any  # TrapIndex — kept untyped here to avoid circular import
    personas: list[Persona]

    # ── planner output ────────────────────────────────────────────────────
    plan: EvaluationPlan

    # ── conductor output (grows turn by turn) ─────────────────────────────
    transcript: Annotated[list[Turn], add]
    current_turn: int

    # ── jury (parallel writes from N personas merged via reducers) ────────
    round_one_scores: Annotated[list[JurorScore], _extend_juror_scores]
    round_two_scores: Annotated[list[JurorScore], _extend_juror_scores]
    metrics_to_revote: list[str]

    # ── consensus output ─────────────────────────────────────────────────
    consensus: Annotated[dict[str, ConsensusResult], _merge_dicts]

    # ── final output ──────────────────────────────────────────────────────
    per_metric: dict[str, float]
    confidence: dict[str, float]
    final_score: float
    certification: str
    findings: list[Finding]
    warnings: list[str]
    summary: str

    # ── config / observability ────────────────────────────────────────────
    consensus_strategy: str       # "independent" | "delphi" | "debate"
    debate_rounds: int
    revote_threshold: float
    scoring_config: Scoring
    on_event: Callable[[Event], None] | None
    cost_usd: float
    tokens_used: int

    # Per-prompt char budget. Auto-detected from the model's context window
    # at Harness construction; can be overridden via Harness(context_budget_tokens=...).
    context_budget_chars: int
