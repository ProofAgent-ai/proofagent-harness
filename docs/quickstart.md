# Quickstart

[← back to docs](README.md)

After [installing](install.md), run your first eval in under a minute.

## The 10-line evaluation

```python
from proofagent_harness import Harness

def my_agent(message: str) -> str:
    # Your agent: any callable that takes a message and returns a string.
    # Wrap your existing LangChain / CrewAI / OpenAI Agents SDK / custom code here.
    return your_llm_call(message)

report = Harness().evaluate(
    my_agent,
    role="customer support agent",
    goal="handle refunds safely",
)

print(report)
```

## Auto-printed scorecard

When `evaluate()` finishes, the harness prints a rich-text scorecard to your terminal:

```
proofagent-harness — Scorecard
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Metric                  ┃     Score ┃ Confidence ┃ Severity ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━┩
│ Task Success            │  9.0 / 10 │       0.90 │ pass     │
│ Hallucination Resistance│  8.0 / 10 │       1.00 │ pass     │
│ Safety                  │ 10.0 / 10 │       1.00 │ pass     │
│ Instruction Following   │  9.0 / 10 │       1.00 │ pass     │
│ Manipulation Resistance │  8.0 / 10 │       0.90 │ pass     │
└─────────────────────────┴───────────┴────────────┴──────────┘

Final score: 8.80 / 10    Certification: SILVER    Tokens: 51,518
```

## What just happened

| Step | What ran | Time | Calls |
|---|---|---:|---:|
| 1 | **Planner** inferred the domain from `role` + `goal`, picked relevant traps | ~3s | 2–3 |
| 2 | **Conductor** ran 8 adversarial turns against `my_agent` | ~15s | 16 |
| 3 | **3 Harness Jurors** scored independently on 5 metrics | ~8s | 15 |
| 4 | **Delphi re-vote** on disputed metrics only | ~3s | ~5 |
| 5 | **Reporter** assembled findings + certification | ~2s | 1 |
| | | **~30s** | **~38** |

See [How it works →](how-it-works.md) for the full pipeline.

## Inspect the full report

The full report object carries everything — transcript turns, Harness Juror reasoning, findings, consensus log:

```python
print(report.final_score)               # 8.8
print(report.certification)             # 'SILVER'
print(report.per_metric)                # {'task_success': 9.0, ...}

# Per-turn transcript
for turn in report.transcript:
    print(turn.turn_index, turn.question, turn.answer)

# Persist
report.to_json("report.json")           # full structured JSON
report.to_markdown("report.md")         # human-readable Markdown
```

## Next steps

- [Why proofagent-harness →](why.md) — what this lib does that single-shot eval libs don't
- [Your agent + AgentContext →](your-agent.md) — make the score deeper by passing in system prompt + knowledge + tools
- [CI integration →](ci-integration.md) — turn this into a pytest assertion
