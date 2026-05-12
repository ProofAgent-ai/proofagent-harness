"""proofagent-harness — open-source test harness for AI agents.

Quickstart:

    from proofagent_harness import Harness

    def my_agent(message: str) -> str:
        return your_llm_call(message)

    report = Harness().evaluate(
        my_agent,
        role="customer support agent",
        goal="handle refunds safely",
    )
    print(report)
"""

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
    # primary
    "Harness",
    # config
    "LLM",
    "Scoring",
    # inputs
    "AgentContext",
    "AgentResponse",
    "AgentCallable",
    # data
    "Trap",
    "Skill",
    "Persona",
    "EvaluationPlan",
    "TurnSpec",
    "Turn",
    "JurorScore",
    "ConsensusResult",
    # output
    "Report",
    "Finding",
    "Severity",
    "Certification",
    "Event",
    # constants
    "CANONICAL_METRICS",
    "METRIC_DESCRIPTIONS",
    "__version__",
]
