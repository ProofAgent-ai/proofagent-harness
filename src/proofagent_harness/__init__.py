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
    # constants
    "CANONICAL_METRICS",
    # config
    "LLM",
    "METRIC_DESCRIPTIONS",
    "AgentCallable",
    # inputs
    "AgentContext",
    "AgentResponse",
    "Certification",
    "ConsensusResult",
    "EvaluationPlan",
    "Event",
    "Finding",
    # primary
    "Harness",
    "JurorScore",
    "Persona",
    # output
    "Report",
    "Scoring",
    "Severity",
    "Skill",
    # data
    "Trap",
    "Turn",
    "TurnSpec",
    "__version__",
]
