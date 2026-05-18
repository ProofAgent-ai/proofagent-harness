# CLI + Recipes

[← back to docs](README.md)

The `proof` CLI ships with the package.

## Core commands

```bash
proof run AGENT_FILE [OPTIONS]      # Run an eval against a Python file exposing `agent`
proof traps list                    # List bundled traps (filter by --family)
proof traps show NAME               # Inspect one trap
proof traps stats                   # Library coverage summary
proof traps validate [PATH]         # Lint trap manifests (default: bundled library)
proof traps install PACK            # pip install a community trap pack
proof traps domains                 # Domain → traps mapping
proof metrics                       # List the 5 canonical metrics
proof version                       # Print the package version
```

`proof --help` and `proof traps --help` show every flag.

## `proof run` — full reference

```bash
proof run my_agent.py \
    --entry agent                       # name of the callable (default: agent)
    --role "customer support agent" \
    --business-case "refund processing" \
    --goal "follow refund policy v2.4" \
    --turns 8 \                         # conductor turn count
    --consensus delphi \                # independent | delphi | debate
    --metrics safety,hallucination_resistance \
    --knowledge ./knowledge/ \
    --llm claude-sonnet-4-6 \           # Harness LLM (any LiteLLM target)
    --seed 42 \
    --json report.json \                # write JSON report
    --markdown report.md \              # write Markdown report
    --quiet                             # suppress live progress UI
```

`my_agent.py` just needs to expose a callable at the top level:

```python
# my_agent.py
def agent(message: str) -> str:
    return my_llm_call(message)
```

## Recipes

### Smoke test — fast pre-PR sanity check (~30s)

```bash
proof run my_agent.py \
    --turns 4 --consensus independent --llm claude-haiku-4-5 \
    --role "customer support" --goal "handle refunds safely"
```

Use in pre-commit hooks or pull-request CI.

### Production-grade evaluation — recommended default (~3-5 min)

```bash
proof run my_agent.py \
    --turns 8 --consensus delphi --seed 42 \
    --role "customer support" --goal "handle refunds safely" \
    --knowledge ./knowledge/ \
    --json report.json --markdown report.md
```

Best signal-per-call ratio. Use as the default release-gate.

### Cheap iteration loop — Haiku for the harness, your agent untouched

```bash
proof run my_agent.py \
    --turns 8 --consensus delphi --seed 42 \
    --llm claude-haiku-4-5
```

Your agent runs whatever it normally runs. The harness uses Haiku so per-run cost drops ~10×.

### Stability check — sample the same agent 3 times

```bash
for i in 1 2 3; do
  proof run my_agent.py --turns 8 --seed $((42 + i)) --json report-$i.json
done
```

Then compare scores to spot variance. Useful before locking a threshold in CI.

### High-stakes / regulated — debate consensus (~10-15 min)

```bash
proof run my_agent.py \
    --turns 15 --consensus debate --seed 42 \
    --role "patient-facing triage agent" \
    --goal "comply with HIPAA + safe escalation" \
    --knowledge ./policies/
```

Strictest verdict. Defensible for regulator-facing audits.

## Inspecting the trap library

```bash
proof traps list                                # all 64 bundled traps
proof traps list --family social_engineering    # filter by family
proof traps show authority_impersonation        # full body
proof traps stats                               # universal / domain-specific / metric coverage
proof traps domains                             # which traps fire for which domains
proof traps validate                            # lint the bundled library
proof traps validate ./my_traps/ --strict       # lint your dir, fail on warnings (CI)
```

See [Bring your own traps →](red-teaming.md) for authoring + validating your own.

## Flags that actually matter

| Flag | Default | Notes |
|---|---|---|
| `--turns` | `8` | `4` smoke · `8` production · `15+` high-stakes |
| `--consensus` | `delphi` | See [How it works → Consensus](how-it-works.md#4-consensus) |
| `--llm` | `claude-sonnet-4-6` | Any LiteLLM target. Set `PROOFAGENT_LLM` env var to change the default. |
| `--seed` | unset | OpenAI / Gemini honor it. Anthropic doesn't yet. |
| `--metrics` | all 5 | Comma-separated subset to skip metrics that don't apply (e.g. `safety,hallucination_resistance`). |
| `--knowledge` | none | Path or directory. Hands the corpus to the hallucination-resistance Harness Juror. |
| `--quiet` | off | Suppress live UI. Useful in non-TTY CI environments. |

See [Configuration →](configuration.md) for every `Harness(...)` knob.
