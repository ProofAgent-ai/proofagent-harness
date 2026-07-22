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

    # Provider/framework-agnostic PerformanceCollector (performance.py) — a shared
    # mutable object the conductor records each turn's latency + optional (answer,
    # usage) into. Declared so LangGraph propagates it to the conductor node; the
    # same object reference is built into Report.performance after the graph runs.
    perf_collector: Any

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
    # v0.6.0 — consensus="debate" only. Every INTERMEDIATE debate round's
    # juror scores (rounds 1..debate_rounds-1), tagged by JurorScore.debate_round,
    # preserved for the audit trail. The FINAL round lands in round_two_scores
    # (it's what finalize_consensus aggregates). Empty for delphi / independent.
    debate_round_scores: Annotated[list[JurorScore], _extend_juror_scores]
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

    assess_context: bool
    """v0.7.0 — OPTIONAL context-engineering assessment toggle. Set from
    evaluate(assess_context=True) / the artifact state_seed; read by
    reporter_node to decide whether to grade the supplied context. MUST be
    declared here so LangGraph propagates it from the initial state through to
    the reporter — an UNDECLARED key is silently dropped between nodes."""

    assess_compliance: bool
    """OPTIONAL compliance-assessment toggle (--assess-compliance). Read by
    compliance_assessor_node (runs after the reporter) to decide whether to map
    the finished run to regulatory frameworks. Off by default; additive — never
    touches per_metric / final_score / certification / the gate."""

    governance_profile: Any
    """OPTIONAL Agent Governance Profile (governance_profile.GovernanceProfile) for
    this run. When present, its risk classification steers trap selection (planner)
    and the context-engineering assessment bar (reporter). None → unchanged."""

    seed: int | None
    """OPTIONAL deterministic seed (Harness(seed=...) / --seed). Read by the
    planner's trap sampler for reproducible trap selection. MUST be declared
    here — an undeclared key is silently dropped by LangGraph, which would
    quietly de-seed the sampler."""
    compliance_frameworks: list[str]
    """Framework ids to assess (from --frameworks or governance's
    /compliance/selection). Empty → the default core set. MUST be declared so
    LangGraph propagates it to compliance_assessor_node (undeclared keys are
    silently dropped between nodes — the reason the env fallback existed)."""

    # Reporter-produced assessments. These MUST be declared channels: the
    # reporter writes them in its return dict, and LangGraph drops any
    # undeclared key, so an undeclared output never reaches _state_to_report.
    compliance: dict[str, Any]
    """Compliance assessment dict produced by reporter_node (see compliance.py)."""

    context_engineering: dict[str, Any]
    """v0.7.0 — context-engineering assessment dict produced by reporter_node."""

    # ── Degradation bookkeeping (MUST be declared channels) ─────────────
    # Accumulated inside a node and RETURNED by conductor / jury nodes;
    # LangGraph silently drops in-place mutations of the state view, so an
    # undeclared counter never reaches the reporter — the reason crash
    # warnings and the conductor fail-fast were dead until these were added.

    _juror_llm_failures: int
    """Count of juror LLM calls that failed or violated the scoring protocol
    (error, missing score, empty audit). Reporter surfaces it in warnings."""

    _agent_crash_count: int
    """Total turns on which the agent under test raised. Reporter warning."""

    _agent_consecutive_crashes: int
    """Consecutive same-type crash chain length; conductor fail-fasts at 3."""

    _agent_last_crash_type: str | None
    """Exception type of the most recent crash (None after a clean turn)."""

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
