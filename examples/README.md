# ProofAgent Harness Examples

Runnable examples for every pattern the harness supports. Each example is
self-contained and prints a final scorecard. This README covers shared setup
once, then a per-example section with what it shows, how to run it, and the
flags that matter.

## Contents

| File | Use case |
|---|---|
| [`01_quickstart.py`](01_quickstart.py) | Canonical 10-line benchmark across LLM families |
| [`02_pytest_integration.py`](02_pytest_integration.py) | Drop-in pytest assertion for CI |
| [`03_stateful_agent_with_response.py`](03_stateful_agent_with_response.py) | Closure-based stateful agent returning `AgentResponse` |
| [`04_with_full_context.py`](04_with_full_context.py) | `AgentContext.from_dir()` auto-discovery |
| [`05_compliance_focused.py`](05_compliance_focused.py) | Restrict scoring to compliance-tagged traps |
| [`06_weak_agent_baseline.py`](06_weak_agent_baseline.py) | Calibration check — verify the harness discriminates |
| [`07_proxy_llm_agent.py`](07_proxy_llm_agent.py) | Self-hosted agent on a local OpenAI-compatible proxy |
| [`08_custom_trap.py`](08_custom_trap.py) | Bring-your-own-trap with `--trap PATH` |
| [`09_asymmetric_single_cell.py`](09_asymmetric_single_cell.py) | Small local Harness LLM vs frontier agent across four bundled domains |
| [`agents/`](agents/) | Four production-style domain agent specs + multi-provider factory |
| [`custom_traps/`](custom_traps/) | Sample trap used by `08_custom_trap.py` |

---

## Common setup (do this once)

### 1. Install the package

```bash
pip install proofagent-harness
```

For source / development checkouts:

```bash
git clone https://github.com/ProofAgent-ai/proofagent-harness
cd proofagent-harness
pip install -e .
```

### 2. Export the API keys you'll use

Mix and match — the agent under test and the Harness LLM can come from
different providers. You only need the keys for the providers you actually
call.

```bash
export OPENAI_API_KEY=sk-...           # gpt-* agents or Harness LLM
export ANTHROPIC_API_KEY=sk-ant-...    # claude-* / anthropic/* agents or Harness LLM
export GEMINI_API_KEY=...              # gemini/* agents or Harness LLM
```

### 3. (Optional) Local OpenAI-compatible proxy for the Harness LLM

Needed only for examples that route the Harness LLM through a local model
(7, 9, and 8 with `--proxy-url`). Any OpenAI-compatible server works:

| Server | Default URL | Notes |
|---|---|---|
| **LM Studio** | `http://localhost:1234/v1` | GUI + CLI (`lms load <model>`). Single-threaded — pair with `--sequential`. |
| **Ollama** | `http://localhost:11434/v1` | `ollama serve` then `ollama pull <model>`. Single-threaded by default. |
| **vLLM** | `http://localhost:8000/v1` | `vllm serve <model>`. Real parallelism, no `--sequential` needed. |
| **mlx-lm** | varies | `mlx_lm.server --model <hf-repo>` on Apple Silicon. |

Verify the proxy is reachable and note the exact model id:

```bash
curl http://localhost:1234/v1/models | python3 -m json.tool
```

The `id` field in the response is the literal string to pass to
`--harness-llm` or `--llm`.

---

## `01_quickstart.py` — canonical benchmark

**What it shows.** The 10-line quickstart with a real agent (default
Anthropic Claude over the AcmeAir refund policy). Supports cross-family
judging — the agent and the Harness LLM (called the "judge" in this
script) can come from different providers.

```bash
# Default (Anthropic agent, Claude judge)
python examples/01_quickstart.py

# Head-to-head benchmark — OpenAI agent, Anthropic judge (cross-family)
python examples/01_quickstart.py --turns 15 --consensus debate \
  --agent-model gpt-4.1 --llm anthropic/claude-haiku-4-5

# Cheap smoke test
python examples/01_quickstart.py --turns 4 --consensus independent \
  --llm anthropic/claude-haiku-4-5

# Route the JUDGE through a local proxy (agent stays cloud)
python examples/01_quickstart.py \
  --proxy-url http://localhost:1234/v1 \
  --llm gemma-4-E4B-it-MLX-8bit --ctx 6000
```

**Key flags.** `--agent-model` (agent LLM, auto-detects provider) ·
`--llm` (Harness LLM / judge) · `--turns` (default 8) ·
`--consensus` (`independent` / `delphi` / `debate`) · `--proxy-url` ·
`--ctx` (juror context budget for small-context proxy models).

---

## `02_pytest_integration.py` — CI assertion

**What it shows.** Drop the harness into a pytest suite as a single
assertion. The test fails if the final score drops below a threshold.

```bash
pytest examples/02_pytest_integration.py
```

No additional flags — the threshold and turn count are in the test body
(edit to taste). Add it to your CI workflow alongside your unit tests.

---

## `03_stateful_agent_with_response.py` — stateful agent + `AgentResponse`

**What it shows.** A closure-based stateful agent (no class required) that
returns `AgentResponse(text, tools_called, retrievals, memory_snapshot)`
so the jury can score tool use, retrievals, and memory drift across turns.

```bash
python examples/03_stateful_agent_with_response.py
```

Edit the script to swap the system prompt, tools, or memory schema. No CLI
flags — values are inlined in the `if __name__ == "__main__":` block.

---

## `04_with_full_context.py` — `AgentContext.from_dir()`

**What it shows.** Ground every juror in your real agent's system prompt,
knowledge base, and tool schemas via `AgentContext.from_dir(...)`. The
script bootstraps a self-contained `examples/my_agent_dir/` on first run
so you can see the expected layout.

```bash
python examples/04_with_full_context.py
```

Expected directory layout (auto-created on first run):

```
examples/my_agent_dir/
├── system_prompt.md
├── knowledge/refund_policy.md
└── tools.json
```

To use your own context: point the script's `EX_DIR` constant at your
agent's directory and remove the `_bootstrap_dir()` call.

---

## `05_compliance_focused.py` — compliance-only scoring

**What it shows.** Restrict adversarial trap coverage to compliance-tagged
families (GDPR, CCPA, HIPAA, PCI, SOX). Useful when you want a focused
regulated-domain audit rather than a general red-team.

```bash
python examples/05_compliance_focused.py
```

The compliance traps are already in the bundled library; this example
demonstrates the API for restricting trap selection. Edit the
`extra_traps=` or `restrict_traps_to=` parameters in the script to filter
to other families.

---

## `06_weak_agent_baseline.py` — calibration check

**What it shows.** A deliberately weak agent (no system prompt, no
context, no memory) on the same proxy LLM as `07_proxy_llm_agent.py`.
Compare the score of this weak agent against the hardened agent in 07 to
verify your harness configuration actually discriminates by agent quality
rather than over-rating everything.

```bash
# Step 1 — run the hardened proxy agent (07)
python examples/07_proxy_llm_agent.py --turns 15 --consensus delphi
# → note final score X

# Step 2 — run this weak agent on the SAME proxy
python examples/06_weak_agent_baseline.py --turns 15 --consensus delphi
# → note final score Y

# Step 3 — interpret the gap (X - Y):
#   ≥ 3 points    well-calibrated
#   1.5-3 points  some discrimination, plateau bias is muting signal
#   < 1.5 points  harness isn't discriminating — investigate
```

**Key flags.** `--turns` (default 15) · `--consensus` (default `delphi`)
· `--llm` (Harness LLM, default `claude-haiku-4-5`).

**Env vars** (for the proxy):

```bash
export PROOFAGENT_PROXY_URL=http://localhost:1234/v1
export PROOFAGENT_PROXY_MODEL=gemma-4-E4B-it-MLX-8bit
export PROOFAGENT_PROXY_KEY=not-required-for-local-proxy
```

---

## `07_proxy_llm_agent.py` — self-hosted AGENT

**What it shows.** Run the AGENT on a self-hosted / local OpenAI-compatible
endpoint (mlx-lm, vLLM, Ollama, LM Studio, corporate proxy). The Harness
LLM stays on whatever you pass to `--llm`.

```bash
# Default — uses env vars below
python examples/07_proxy_llm_agent.py

# Longer debate run with a specific Harness LLM
python examples/07_proxy_llm_agent.py --turns 25 --consensus debate \
  --llm anthropic/claude-haiku-4-5
```

**Env vars** (configure your proxy here):

```bash
export PROOFAGENT_PROXY_URL=http://localhost:1234/v1
export PROOFAGENT_PROXY_MODEL=gemma-4-E4B-it-MLX-8bit
export PROOFAGENT_PROXY_KEY=not-required-for-local-proxy
```

**Key flags.** `--turns` · `--consensus` · `--llm` (Harness LLM).

---

## `08_custom_trap.py` — bring-your-own-trap

**What it shows.** Merge your own adversarial traps into the bundled
library via `Harness(extra_traps=[...])`. Inherits the full multi-provider
agent + judge from `01_quickstart.py` and adds one new flag: `--trap PATH`
(directory of `.md` trap manifests, or a single `.md` file).

```bash
# 0) Wiring sanity check — no API calls
python examples/08_custom_trap.py --list-only

# 1) Default — bundled demo trap (custom_traps/refund_chargeback_threat.md)
python examples/08_custom_trap.py --turns 8

# 2) Your own trap pack
python examples/08_custom_trap.py --trap ./my_traps/ --turns 8

# 3) Single trap file + custom LLM
python examples/08_custom_trap.py --trap ./my_traps/attack.md \
  --turns 8 --consensus debate --llm gpt-5.5
```

**Key flags.** `--trap PATH` (extra traps to merge) · `--turns` ·
`--consensus` · `--llm` (Harness LLM) · `--agent-model` ·
`--proxy-url` · `--ctx` · `--list-only`.

**Authoring traps.** A trap is a single `.md` file with YAML frontmatter
plus `# Pattern`, `# Seed examples`, `# Pass criteria`, `# Fail criteria`
sections. Full spec in [`docs/TRAP_MANIFEST.md`](../docs/TRAP_MANIFEST.md).
Validate before running:

```bash
proof traps validate ./my_traps/attack.md
proof traps validate --strict          # warnings = errors (CI)
```

---

## `09_asymmetric_single_cell.py` — multi-domain asymmetric evaluation

**What it shows.** Evaluate one of four bundled production-style domain
agents (customer support, medical triage, code generation, privacy /
security) under any Harness LLM tier: cheap cloud, frontier cloud, or a
local 4B model on LM Studio. Reproduces the headline cohort cells from the
paper. The four bundled agent specs live in [`agents/`](agents/).

### Scenario A — cheap cloud smoke test

5-turn sanity check, ~$0.30, ~3 min. Use this before any longer run.

```bash
python examples/09_asymmetric_single_cell.py \
  --agent       medical_triage_assistant \
  --agent-llm   gpt-4.1-mini \
  --harness-llm anthropic/claude-haiku-4-5 \
  --turns       5 \
  --seed        42 \
  --consensus   debate
```

### Scenario B — frontier reference (Large Harness)

Opus 4.7 evaluating a GPT-5.5 agent, ~$3-5, ~10 min.

```bash
python examples/09_asymmetric_single_cell.py \
  --agent       customer_support_agent \
  --agent-llm   gpt-5.5 \
  --harness-llm anthropic/claude-opus-4-7 \
  --turns       25 --seed 42 --consensus debate
```

### Scenario C — asymmetric local (small local Harness LLM)

The paper's headline asymmetric cell: a 4B local Gemma model (LM Studio)
evaluating a frontier-class agent. ~$0, ~30 min.

```bash
# 1. Load Gemma in LM Studio with 8K context
lms get  mlx-community/gemma-4-E4B-it-MLX-8bit
lms load mlx-community/gemma-4-E4B-it-MLX-8bit --context-length 8192

# 2. Verify
curl http://localhost:1234/v1/models | python3 -m json.tool

# 3. Run
python examples/09_asymmetric_single_cell.py \
  --agent          medical_triage_assistant \
  --agent-llm      gpt-5.5 \
  --harness-llm    gemma-4-E4B-it-MLX-8bit \
  --proxy-url      http://localhost:1234/v1 \
  --turns          25 --seed 42 --consensus debate \
  --context-budget 6000 \
  --sequential
```

Two flags are mandatory for the local path:
- `--context-budget 6000` — Gemma's working context is ~8K; the pre-flight
  check rejects the run without this.
- `--sequential` — LM Studio serves one request at a time; without this,
  parallel juror calls queue and time out.

### Scenario D — sweep all four agents

```bash
for AGENT in medical_triage_assistant customer_support_agent \
             code_generation_agent privacy_security_agent; do
  python examples/09_asymmetric_single_cell.py \
    --agent       "$AGENT" \
    --agent-llm   gpt-5.5 \
    --harness-llm anthropic/claude-haiku-4-5 \
    --turns       25 --seed 42 --consensus debate \
    --output-dir  ./results/sweep_${AGENT}
done
```

### Scenario E — wiring check (no API calls)

```bash
python examples/09_asymmetric_single_cell.py \
  --agent       customer_support_agent \
  --agent-llm   gpt-5.5 \
  --harness-llm anthropic/claude-haiku-4-5 \
  --turns       25 --seed 42 --consensus debate \
  --list-only
```

### CLI flags

| Flag | Meaning |
|---|---|
| `--agent` | Bundled agent name (`customer_support_agent`, `medical_triage_assistant`, `code_generation_agent`, `privacy_security_agent`) or a path to your own JSON spec. |
| `--agent-llm` | Model powering the agent under test. Auto-detects provider: `gpt-*` → OpenAI, `anthropic/claude-*` → Anthropic, `gemini/*` → LiteLLM. |
| `--harness-llm` | Model powering the Harness pipeline. Cloud examples: `anthropic/claude-opus-4-7`, `anthropic/claude-haiku-4-5`, `gpt-5.5`. Local examples (with `--proxy-url`): `gemma-4-E4B-it-MLX-8bit`. |
| `--proxy-url` | OpenAI-compatible URL for a local Harness proxy. Omit for cloud Harness LLMs. |
| `--turns` | Number of adversarial conductor turns. Default 25 (paper cohort). |
| `--seed` | Random seed. Default 42. |
| `--consensus` | `independent` (cheapest) · `delphi` (balanced) · `debate` (strictest, paper default). |
| `--context-budget` | Juror prompt token budget. Required for small-context proxy models (`6000` for 8K-context Gemma 4B). |
| `--sequential` | Serialize juror LLM calls. Required for single-threaded local proxies. No effect on cloud Harness LLMs. |
| `--output-dir` | Where to write reports. Default `./results/asymmetric_<timestamp>/`. |
| `--list-only` | Print resolved config and exit without spending tokens. |
| `--quiet` | Suppress per-turn progress output. |

### Authoring your own agent spec

Drop a `.json` file into [`agents/`](agents/) (or anywhere; the runner
accepts absolute paths) following the schema documented in
[`agents/README.md`](agents/README.md). Then pass its name to `--agent`.

---

## Cost and runtime guidance

Rough numbers per cell at `--turns 25 --consensus debate` (3-persona):

| Harness LLM | Cost / cell | Wall clock | Notes |
|---|---|---|---|
| `anthropic/claude-haiku-4-5` | ~$1-2 | 5-10 min | Best cheap cloud option. |
| `anthropic/claude-opus-4-7` | ~$3-5 | 10-15 min | Paper's Large Harness reference. |
| `gpt-5.5` | ~$2-4 | 10-15 min | Frontier OpenAI. |
| `gpt-4.1-mini` | ~$0.50 | 5-10 min | Cheapest cloud, fine for smoke tests. |
| local Gemma 4B (LM Studio) | $0 | 25-45 min | Paper's Small Harness. Requires `--sequential` + `--context-budget 6000`. |
| local Qwen 2.5 7B / Llama 3.1 8B | $0 | 30-60 min | Stronger small Harness alternatives. |

Lower the cost / time by dropping `--consensus debate` to `delphi` (~1.5×
instead of 3-5× per turn) or lowering `--turns` to 8-15 for development.

---

## Reports

Every example writes structured reports under `./results/` (or
`--output-dir`):

- `<run-id>.json` — full evidence-linked transcript, per-juror scores,
  consensus log, raised findings, metadata.
- `<run-id>.md` — human-readable scorecard, per-metric breakdown, raised
  findings with rationale and recommendation.

The terminal also prints the final score, certification band, and
per-metric table at the end of every run.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: proofagent_harness` | Package not installed in the active Python | `pip install proofagent-harness` (or activate the right venv) |
| `LLMNotConfiguredError: API key missing` | Forgot to `export` the relevant API key | Export the key in the same terminal that runs the script |
| `LLM Provider NOT provided. Pass in the LLM provider...` | LiteLLM can't infer provider from model name | Prefix the model: `anthropic/claude-haiku-4-5`, `gemini/gemini-2.5-pro` |
| `the configured harness LLM cannot handle the context size` | Local proxy loaded at too-small context length | Reload the model with a larger `--context-length` AND/OR drop `--turns` |
| `Error code: 400 — model has crashed` | Local proxy OOM (model + KV cache > available RAM) | Lower the context length on the proxy; close other apps; consider a smaller quant |
| All juror calls time out at 600s+ | Local single-threaded proxy can't handle parallel jury | Add `--sequential` |
| Final score is mid-band (~5-7) with many `SOFT_FAIL` audit lines | Harness LLM too small to parse the debate transcript reliably | Use a larger Harness LLM (≥ 7B for local, or any cloud frontier) |
| `gpt-5.x` API returns "unsupported parameter" | OpenAI dropped `temperature` / renamed `max_tokens` on reasoning models | The factory in `agents/factory.py` already handles this; for custom scripts, drop `temperature` and use `max_completion_tokens` |

---

## See also

- Top-level [`README.md`](../README.md) — package overview, install, quickstart
- [`agents/README.md`](agents/README.md) — agent spec schema + authoring
  your own
- [`docs/TRAP_MANIFEST.md`](../docs/TRAP_MANIFEST.md) — trap spec for
  example 08
- [`notebooks/`](../notebooks/) — end-to-end walkthroughs in Jupyter
