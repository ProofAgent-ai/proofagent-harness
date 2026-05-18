# Reproducibility

[← back to docs](README.md)

LLM evaluations are inherently noisy. The harness minimizes unnecessary variance and gives you knobs to dial in determinism where it matters.

## What's already deterministic by default

- **Harness Jurors run at `temperature=0`** — same transcript always yields the same scores
- **Planner classification (domain inference + weaving) runs at `temperature=0`** — same `role` + `goal` always picks the same traps
- **Test ordering is stable** — trap selection is sorted by name within each family before the planner picks
- **Bundled trap content is byte-identical** between runs — the normalizer (`scripts/normalize_traps.py`) asserts this is true

## What's intentionally not deterministic

- **Conductor question-crafting** runs at moderate temperature. Adversarial creativity matters here — we want different attack angles to surface different failure modes. If a single deterministic question fails to catch a regression, we want a slightly different angle to find it next time.
- **Custom-trap generation** (when used) also uses moderate temperature for the same reason.

## Pin everything you can

```python
from proofagent_harness import Harness

Harness(
    llm="gpt-4.1",            # OpenAI honors seeds; Anthropic doesn't yet
    seed=42,
    turns=8,
    consensus="delphi",
)
```

With this combo:

| Provider | What `seed=42` actually does |
|---|---|
| OpenAI (GPT-4.1, GPT-4o, …) | Deterministic decoding — same input → same output across runs |
| Gemini (1.5 Pro, 1.5 Flash) | Deterministic decoding |
| Anthropic (Claude) | **Ignored** — Anthropic doesn't yet support a `seed` parameter |
| Bedrock (Anthropic via AWS) | Partial — depends on the underlying model |
| Ollama / vLLM | Depends on the served model |

## Expect ±0.5 score variance on Anthropic

Without a seed parameter, Anthropic's API has small per-call variance that propagates into the Harness Jurors' scoring. **Plan for ±0.5 score variance on Anthropic-backed evals.**

To get tighter than that:

- Switch the **Harness LLM** to OpenAI / Gemini + `seed=42` (your agent can still be on Anthropic — they're independent)
- OR run the same eval N times and report the **median + IQR** as your stability number

```python
import statistics
runs = [
    Harness(llm="claude-sonnet-4-6", turns=8).evaluate(my_agent, role="...", goal="...").final_score
    for _ in range(5)
]
print(f"median: {statistics.median(runs):.2f}  IQR: {statistics.quantiles(runs)[2] - statistics.quantiles(runs)[0]:.2f}")
```

This is the right pattern for **any** LLM-as-judge evaluation — single-run scores are point estimates, distributions are the truth.

## Pinning model versions

LiteLLM model strings are usually stable, but new versions ship under new names. Pin explicitly when reproducibility matters:

```python
# Pinned to a specific snapshot
Harness(llm="claude-sonnet-4-6")
Harness(llm="gpt-4.1-2025-04-15")
Harness(llm="gemini/gemini-1.5-pro-001")

# Not pinned — silently rolls forward as providers ship updates
Harness(llm="claude-sonnet-latest")   # ← avoid for regression testing
Harness(llm="gpt-4o-mini")            # ← OpenAI rolls these
```

## Regression-testing pattern

The strongest stability signal is comparing the same agent across releases on the same harness config:

```python
# baseline
report_v1 = Harness(llm="gpt-4.1", seed=42, turns=8).evaluate(agent_v1, role="...", goal="...")
report_v1.to_json("baselines/v1.json")

# after a model swap or prompt change
report_v2 = Harness(llm="gpt-4.1", seed=42, turns=8).evaluate(agent_v2, role="...", goal="...")

assert report_v2.final_score >= report_v1.final_score - 0.3, \
    "Regression: agent_v2 dropped >0.3 vs baseline"
```

That's the harness-equivalent of `npm test` — fast, deterministic, gateable.

## Next

- [Configuration →](configuration.md) — every knob in one place
- [CI integration →](ci-integration.md) — pytest assertion pattern
