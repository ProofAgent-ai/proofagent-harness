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
    AgentArtifact,
    AgentArtifactBundle,
    AgentCallable,
    AgentContext,
    AgentResponse,
    Certification,
    ChunkingPolicy,
    ConsensusResult,
    EvaluationPlan,
    Event,
    Finding,
    JurorScore,
    KnowledgeCorpus,
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

# LAZY, so importing the package does not drag in the whole runtime.
#
# `Harness` pulls in the LangGraph state machine and `LLM` pulls in litellm — a combined
# multi-hundred-megabyte dependency tree that nothing needs in order to READ a report or build a
# record from one. Anything consuming this package for analysis (a CI script, a report renderer,
# the governance platform deriving a record from an uploaded archive) can now install it without
# the orchestration stack, and `import proofagent_harness` stays fast for everyone else.
#
# PEP 562: resolved on first attribute access, so `from proofagent_harness import Harness` behaves
# exactly as before.
_LAZY: dict[str, str] = {
    "Harness": "proofagent_harness.harness",
    "LLM": "proofagent_harness.llm",
    "LLMError": "proofagent_harness.llm",
    "LLMJSONStructureError": "proofagent_harness.llm",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module), name)
    globals()[name] = value  # cache, so the import cost is paid once
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | set(__all__))

__all__ = [
    "CANONICAL_METRICS",
    "LLM",
    "METRIC_DESCRIPTIONS",
    "AgentArtifact",
    "AgentArtifactBundle",
    "AgentCallable",
    "AgentContext",
    "AgentResponse",
    "Certification",
    "ChunkingPolicy",
    "ConsensusResult",
    "EvaluationPlan",
    "Event",
    "Finding",
    "Harness",
    "JurorScore",
    "KnowledgeCorpus",
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
