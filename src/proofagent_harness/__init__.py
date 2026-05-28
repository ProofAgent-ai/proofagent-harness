"""proofagent-harness — the open-source, domain-aware test harness for AI agents.

Run multi-turn adversarial evaluations with jury-based scoring across
production-critical metrics (hallucination, policy compliance, drift,
tool use, manipulation resistance). Bring your own LLM. Bring your own
traps. Run locally, in CI, or scale through ProofAgent Platform.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
Original author: Dr. Fouad Bousetouane.
See the LICENSE and NOTICE files in the source distribution for full terms.
"""

from __future__ import annotations

__author__    = "Dr. Fouad Bousetouane"
__email__     = "fouad@proofagent.ai"
__copyright__ = "Copyright 2025-2026 ProofAI LLC"
__license__   = "Apache-2.0"

from proofagent_harness.harness import Harness
from proofagent_harness.llm import LLM, LLMError, LLMJSONStructureError
from proofagent_harness.loaders import (
    TrapIndex,
    load_personas,
    load_skills,
    load_trap_index,
    load_traps,
)
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

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("proofagent-harness")
except Exception:
    __version__ = "0.0.0+unknown"

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
    "LLMError",
    "LLMJSONStructureError",
    "Persona",
    "Report",
    "Scoring",
    "Severity",
    "Skill",
    "Trap",
    "TrapIndex",
    "Turn",
    "TurnSpec",
    "__author__",
    "__copyright__",
    "__email__",
    "__license__",
    "__version__",
    "load_personas",
    "load_skills",
    "load_trap_index",
    "load_traps",
]
