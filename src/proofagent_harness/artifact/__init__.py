"""Artifact-mode pipeline — single-shot jury evaluation for pre-generated artifacts.

Use this when you already have output (code, business plan, architecture doc,
report) and want the harness juror panel to score it WITHOUT running an
adversarial multi-turn conversation. Same jury, same metrics, same consensus —
just one synthetic turn built from the artifact.

Architecture
------------

    user-supplied artifact + knowledge corpus + context
                          │
                          ▼
            ┌─────────────────────────┐
            │  ArtifactRunner         │   ← orchestrates this module
            │  - convert artifact     │      converters.convert_to_text()
            │  - load corpus          │      loader.load_corpus()
            │  - build synthetic Turn │
            │  - invoke jury graph    │
            └────────────┬────────────┘
                         ▼
              same jury + consensus + reporter
              (re-uses the existing LangGraph nodes,
               just with no planner / conductor stages)

Public surface
--------------

    proofagent_harness.AgentArtifact      — the artifact payload
    proofagent_harness.KnowledgeCorpus    — the source documents
    proofagent_harness.ChunkingPolicy     — chunking knobs (v0.5.1 surface)

All wiring is internal — users call:

    Harness(mode="artifact", llm=...).evaluate(
        artifact=AgentArtifact(...),
        knowledge=KnowledgeCorpus(...),
        role="...",
        business_case="...",
    )
"""

from __future__ import annotations
