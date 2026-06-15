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
    pin_traps: list[str]
    personas: list[Persona]

    plan: EvaluationPlan
    # v0.5.0 — planner visibility (client report B2/B3): which domains were
    # inferred + a trap-selection summary (loaded / selected / not-selected),
    # surfaced into the report metadata so users can see WHY traps fired.
    plan_domains: list[str]
    plan_trap_summary: dict

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
    technical_issues: list[Finding]
    warnings: list[str]
    summary: str
    # v0.5.0 — executive synthesis produced by reporter_node. Declared here
    # so LangGraph keeps them in state (otherwise the returned keys are
    # dropped and never reach the Report).
    executive_summary: str
    production_ready: str
    top_risk: str

    consensus_strategy: str
    debate_rounds: int
    revote_threshold: float
    scoring_config: Scoring
    on_event: Callable[[Event], None] | None
    cost_usd: float
    tokens_used: int

    context_budget_chars: int

    # ── v0.5.0 artifact-mode-only state ────────────────────────────────
    # These keys are only set when mode == "artifact"; multi-turn nodes
    # ignore them entirely. Kept as Optional/missing-key tolerant.

    mode: str
    """Evaluation mode marker ('multi_turn' or 'artifact'). Read by the
    juror prompt builder to swap rubrics + audit protocols."""

    artifact_type: str
    """Artifact type tag (BRD, code, business_plan, etc.). Drives
    type-specific rubric pack selection."""

    trusted_references: list[str]
    """Pre-declared entities the juror should not flag as hallucinations."""

    validation_assertions: list[str]
    """User-supplied claims the juror must evaluate explicitly."""

    agent_execution_evidence: str
    """Distilled agent trace (.log/.jsonl summary) the juror reads as
    EVIDENCE for verification, not as grounding corpus."""

    domain: str
    """Domain glossary pack to inject ('airline', 'healthcare', etc.)."""

    artifact_custom_rubric: dict[str, str]
    """Per-artifact user-supplied rubric (extends/replaces built-in)."""

    artifact_custom_rubric_mode: str
    """How `artifact_custom_rubric` combines: 'extend' | 'replace' | 'replace_all'."""

    site_custom_rubrics: dict[str, Any]
    """Harness-level custom_rubrics={type: rubric_dict} map."""
