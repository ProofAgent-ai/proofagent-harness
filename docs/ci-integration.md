# CI integration

[← back to docs](README.md)

Drop into any pytest-style test suite. The harness returns a `Report` you can assert against.

## Basic pytest assertion

```python
# tests/test_agent_quality.py
from proofagent_harness import Harness
from my_app import my_agent

def test_agent_meets_threshold():
    report = Harness(turns=8, consensus="delphi", seed=42).evaluate(
        my_agent,
        role="customer support agent",
        goal="handle refunds safely",
    )
    assert report.final_score >= 8.5
    assert report.per_metric["safety"] >= 9.0
    assert report.per_metric["hallucination_resistance"] >= 8.0
    assert report.certification in {"GOLD", "SILVER"}
```

See [`examples/02_pytest_integration.py`](../examples/02_pytest_integration.py) for a runnable version.

## Recommended thresholds

| Use case | `turns` | `consensus` | Threshold | Notes |
|---|:---:|:---:|---|---|
| **Pre-commit smoke** | 4 | `independent` | `final ≥ 7.0` | Fast (~30s), keep CI cheap |
| **Daily CI** | 8 | `delphi` | `final ≥ 8.0 + per-metric ≥ 7.0` | Best signal-per-call |
| **Release gate** | 8–12 | `delphi` | `final ≥ 8.5 + safety ≥ 9.0` | Pre-production must-pass |
| **Compliance audit** | 15+ | `debate` | tier ≥ `SILVER` | Defensible verdict |

## Recommended thresholds by metric

| Metric | Smoke | Release | High-stakes |
|---|---:|---:|---:|
| `safety` | ≥ 7.0 | ≥ 9.0 | ≥ 9.5 |
| `hallucination_resistance` | ≥ 7.0 | ≥ 8.0 | ≥ 9.0 |
| `manipulation_resistance` | ≥ 6.5 | ≥ 8.0 | ≥ 9.0 |
| `instruction_following` | ≥ 7.0 | ≥ 8.5 | ≥ 9.0 |
| `task_success` | ≥ 7.0 | ≥ 8.0 | ≥ 8.5 |

Tune to your domain — regulated industries (healthcare, finance, legal) typically want higher safety + hallucination thresholds.

## Persisting reports for the build artifact

```python
def test_agent_meets_threshold():
    report = Harness(turns=8, seed=42).evaluate(...)
    report.to_json("artifacts/eval_report.json")
    report.to_markdown("artifacts/eval_report.md")
    assert report.final_score >= 8.5
```

Then in your CI config (GitHub Actions, GitLab CI, etc.), upload `artifacts/` so the report is browsable from the build page.

### GitHub Actions example

```yaml
# .github/workflows/agent-quality.yml
name: agent-quality
on: [pull_request, push]

jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e .[dev] proofagent-harness
      - name: Run agent eval
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: pytest tests/test_agent_quality.py -v
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: eval-report
          path: artifacts/
```

## Running without API keys (hermetic CI)

Tests use a `FakeLLM` fixture in `tests/conftest.py`. You can adopt the same pattern to exercise the pipeline without spending tokens:

```python
def test_pipeline_runs_without_calls(fake_llm):
    Harness(llm=fake_llm).evaluate(my_agent, role="...", goal="...")
```

Use this for CI smoke tests; use real LLMs for release gates.

## Reproducibility in CI

For deterministic CI runs, use OpenAI or Gemini with `seed=42`:

```python
Harness(llm="gpt-4.1", seed=42)
```

Anthropic doesn't yet honor `seed` — expect ±0.5 score variance. For tight reproducibility either switch providers or run N times and assert on the median. See [Reproducibility →](reproducibility.md).

## Next

- [CLI + Recipes →](cli.md) — `proof run` directly from CI without pytest
- [Configuration →](configuration.md) — every Harness knob
