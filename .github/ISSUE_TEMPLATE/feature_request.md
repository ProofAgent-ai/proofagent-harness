---
name: Feature request
about: Suggest a new trap, scoring rubric, conductor technique, or harness capability
title: "[feature] "
labels: enhancement
---

## What you want

<!-- One-paragraph description of the capability. -->

## Why it matters / what it catches

<!-- What real-world failure mode does this address? Cite a documented incident,
production bug, paper, or threat model if applicable. The harness is biased
toward features that have empirical grounding. -->

## Proposed implementation

<!-- Where it lives in the pipeline:
   - New trap → which family? src/proofagent_harness/data/traps/<family>/<name>.md
   - New scoring criterion → which metric's rubric?
                              src/proofagent_harness/data/skills/scoring/<metric>.md
   - New conductor technique → conducting.md
   - New defect detector → conductor.py:_detect_defects
   - New schema field → schemas.py
-->

## Acceptance criteria

<!-- How will we know it works? E.g.:
- "On the weak agent, this trap should produce a SOFT_FAIL on at least
   2 of the 3 jurors"
- "On the calibration benchmark, the discrimination gap improves by X points"
-->

## Related issues / runs

<!-- Link to runs (results/*.json), prior issues, papers, etc. -->
