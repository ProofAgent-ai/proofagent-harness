"""proofagent-harness — open-source test harness for AI agents."""

from __future__ import annotations

from proofagent_harness.harness import Harness
from proofagent_harness.llm import LLM
from proofagent_harness.schemas import (
    CANONICAL_METRICS,
    METRIC_DESCRIPTIONS,
    AgentCallable,
    AgentContext,
    AgentResponse,
    Certification,
    ConsensusResult,
    EvaluationPlan,
    Event,
    Finding,
    JurorScore,
    Persona,
    Report,
    Scoring,
    Severity,
    Skill,
    Trap,
    Turn,
    TurnSpec,
)

__version__ = "0.1.0"

__all__ = [
    "CANONICAL_METRICS",
    "LLM",
    "METRIC_DESCRIPTIONS",
    "AgentCallable",
    "AgentContext",
    "AgentResponse",
    "Certification",
    "ConsensusResult",
    "EvaluationPlan",
    "Event",
    "Finding",
    "Harness",
    "JurorScore",
    "Persona",
    "Report",
    "Scoring",
    "Severity",
    "Skill",
    "Trap",
    "Turn",
    "TurnSpec",
    "__version__",
]
