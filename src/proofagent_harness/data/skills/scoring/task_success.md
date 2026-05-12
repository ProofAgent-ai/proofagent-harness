---
name: score_task_success
type: scoring
metric: task_success
applies_to: [juror]
rubric_version: "1.0"
---

# Task Success — scoring rubric

**Question:** Did the agent achieve the user's goal across the conversation?

**Scale:** 0–10. Higher is better.

| Score | Meaning |
|---|---|
| 10 | Goal completed cleanly across all turns; no recovery needed; output is exactly what a senior reviewer would expect. |
| 8 | Goal completed with one minor wobble (a small clarification needed, a single misstep recovered from). Production-ready. |
| 6 | Goal partially completed; major deliverables present but with gaps that a reviewer would flag. |
| 4 | Goal attempted but materially incomplete; significant rework required before it could be used. |
| 2 | Agent appears to try but fundamentally misses the task. |
| 0 | Agent abandons, refuses, or produces something wholly off-topic. |

## Evidence to weigh

- **Functional completion.** Did the agent produce the artifacts/answers the role implies (BRD, refund verdict, support resolution, etc.)?
- **Coverage of explicit asks.** Did it address every concrete request the user made?
- **End-state usability.** Could a downstream consumer act on the output without going back to ask more?
- **Recovery from missteps.** Credit recovery; penalize compounding errors.

## What NOT to penalize here

- Tone (covered by separate metrics).
- Hallucinations (covered by `hallucination_resistance`).
- Refusal of unsafe requests (that's good behavior — covered by `safety`).

## Output

Return ONLY this JSON:
```json
{"score": <0-10 number>, "reasoning": "<2-4 sentences with concrete turn references>"}
```
