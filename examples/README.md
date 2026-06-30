# ProofAgent Harness — Examples

Runnable, self-contained examples for every pattern the harness supports. Each
one is a single file you can run as-is after cloning, each writes a standard
local report (`results/<run-id>.json` + `.md`), and **each runs fully offline by
default** — no ProofAgent account, no network. Pass `--upload` to *also* push the
finished run to the ProofAgent Governance dashboard and get back a release-gate
decision. The examples were curated so every harness knob is a real `argparse`
flag — the per-example **Arguments** tables below are taken verbatim from each
script's `--help`.

> New here? Start with [`01_quickstart.py`](01_quickstart.py) (multi-turn) or
> [`04_artifact_eval.py`](04_artifact_eval.py) (score a pre-written doc), then
> [`02_agent_with_tools.py`](02_agent_with_tools.py) for the full
> "evaluate my real tool-using agent" template.

---

## Common setup (do this once)

### 1. Install

```bash
pip install proofagent-harness
```

Artifact mode for **non-Markdown** inputs (PDF / DOCX / HTML / images /
notebooks) needs extra parsers — install the optional extra:

```bash
pip install "proofagent-harness[artifact]"   # only needed for 04/05 on .pdf/.docx/.html
```

Source / development checkout:

```bash
git clone https://github.com/ProofAgent-ai/proofagent-harness
cd proofagent-harness
pip install -e ".[artifact]"
```

### 2. Export the provider keys you'll use

The agent under test and the harness LLM (the jury) can come from **different
providers** — export only the keys for the providers you actually call.

```bash
export OPENAI_API_KEY=sk-...           # gpt-* agents or harness LLM
export ANTHROPIC_API_KEY=sk-ant-...    # claude-* / anthropic/* agents or harness LLM
export GEMINI_API_KEY=...              # gemini/* agents or harness LLM
```

> **Provider prefixes.** If LiteLLM can't infer the provider from a bare model
> name, prefix it: `anthropic/claude-haiku-4-5`, `gemini/gemini-2.5-pro`. OpenAI
> `gpt-*` ids work unprefixed.

### 3. (Optional) Local OpenAI-compatible proxy for the harness LLM

Only the proxy examples (07, 08) and any run that passes `--proxy-url` (01, 06)
route the **harness LLM** through a local model. Any OpenAI-compatible server
works:

| Server | Default URL | Notes |
|---|---|---|
| **LM Studio** | `http://localhost:1234/v1` | GUI + CLI (`lms load <model>`). Single-threaded — pair with `--sequential` where the example offers it. |
| **Ollama** | `http://localhost:11434/v1` | `ollama serve` then `ollama pull <model>`. Single-threaded by default. |
| **vLLM** | `http://localhost:8000/v1` | `vllm serve <model>`. Real parallelism. |
| **mlx-lm** | varies | `mlx_lm.server --model <hf-repo>` on Apple Silicon. |

Verify the proxy and note the exact model id (the `id` field is the literal
string to pass as the model name):

```bash
curl http://localhost:1234/v1/models | python3 -m json.tool
```

### 4. Pushing to the dashboard

Examples **01–09** and **11** share one uniform **governance upload** flag group,
registered by [`_dashboard.py:add_governance_upload_args`](_dashboard.py). It is
**off by default** — runs stay offline and write only the local report. Add
`--upload` to push the *finished* report to the **ProofAgent Governance API**
(the same path as `proof run --upload`): it builds the payload, POSTs it, prints
the gate decision (`pass` / `review` / `block`) and a dashboard URL, and works
for **both** multi-turn and artifact runs.

| Flag | Default | Meaning |
|---|---|---|
| `--upload` / `--no-upload` | `--no-upload` (offline) | Push the finished run to the Governance API and print the gate decision. |
| `--api-key KEY` | env `PROOFAGENT_API_KEY` | Governance API key. The flag wins over the env var. |
| `--agent NAME` | per-example | Logical agent name — groups runs + regressions in the dashboard. |
| `--agent-version VER` | none | Version / git ref of the agent under test. |
| `--profile SLUG` | none | Governance profile slug to gate against (e.g. `airline_customer_support`). |
| `--fail-on {pass,review,block}` | `block` | Which gate decision fails the build (advisory exit code; examples 01–10 do not `sys.exit` on it, 11 does). |
| `--source {local,ci_cd,manual,api,scheduled}` | `local` | Origin of the run, recorded in the dashboard. |

Every `--upload` run goes to **ProofAgent Cloud**, so you only need a key:

```bash
# either pass the flag…
python examples/01_quickstart.py --upload --api-key pa_live_...
# …or export it and just pass --upload
export PROOFAGENT_API_KEY=pa_live_...
python examples/01_quickstart.py --upload
```

Get a key from the Governance dashboard → **Settings → API Keys**.

---

## Contents

| File | What it shows | Mode | Pushes via |
|---|---|---|---|
| [`01_quickstart.py`](01_quickstart.py) | Canonical N-turn adversarial eval of a refund agent with full `AgentContext`; cross-family agent vs harness LLM | multi-turn | `--upload` |
| [`02_agent_with_tools.py`](02_agent_with_tools.py) | The reference for evaluating **your** tool-using agent — real OpenAI function-calling agent, tool schemas + knowledge handed to the jury | multi-turn | `--upload` |
| [`03_full_context.py`](03_full_context.py) | Ground every juror in your agent's real contract via `AgentContext.from_dir()` (system prompt + knowledge + tools) | multi-turn | `--upload` |
| [`04_artifact_eval.py`](04_artifact_eval.py) | Score a **pre-generated** artifact (BRD / code / report / arch doc) against a knowledge corpus — single-turn, no live agent | artifact | `--upload` |
| [`05_local_report.py`](05_local_report.py) | Run fully offline and write the one standard JSON + Markdown report; supports **both** modes | both | `--upload` |
| [`06_custom_traps.py`](06_custom_traps.py) | Bring-your-own adversarial traps merged into the bundled library via `--trap` | multi-turn | `--upload` |
| [`07_proxy_llm.py`](07_proxy_llm.py) | Evaluate an agent served by a local / self-hosted OpenAI-compatible proxy | multi-turn | `--upload` |
| [`08_live_trace.py`](08_live_trace.py) | **Observability** — live per-turn trace (trap card + Q + A + cumulative coverage) for debugging *why* an agent failed | multi-turn | `--upload` |
| [`09_regression.py`](09_regression.py) | **Regression** — sweep versions of one agent offline; per-metric deltas, optionally pushed under one agent name | multi-turn | `--upload` |
| [`10_pytest_ci.py`](10_pytest_ci.py) | Drop-in **pytest** assertion for CI; thresholds via env vars; optional governance gate | multi-turn | helper (env) |
| [`11_governance_gate.py`](11_governance_gate.py) | **Governance gate** — turn a saved report into a release decision (pass / review / block); no LLM key | n/a (reads a report) | `--upload` |
| [`report_viewer.py`](report_viewer.py) | Utility — render a saved report `.json` as a standalone offline HTML dashboard | n/a | — |
| [`_dashboard.py`](_dashboard.py) | Helper — the shared `--upload` flag group + `push_to_dashboard()` (imported, not run) | n/a | — |
| [`agents/`](agents/) | Five production-style domain agent specs + a multi-provider factory (used by 08) | — | — |
| [`sample_artifacts/library_brd/`](sample_artifacts/library_brd/) | Bundled fictional BRD + knowledge corpus used by `04_artifact_eval.py` | — | — |
| [`custom_traps/`](custom_traps/) | Sample trap used by `06_custom_traps.py` | — | — |

---

## `01_quickstart.py` — canonical multi-turn benchmark

**What it shows.** An N-turn adversarial evaluation of an OpenAI `gpt-4.1` refund
agent with a full `AgentContext` (system prompt + tools + knowledge corpus). The
agent and harness LLM can come from different providers (cross-family judging);
`--proxy-url` redirects only the harness LLM to a local proxy.

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Offline (default — Anthropic Sonnet jury vs the gpt-4.1 agent), writes a local report
python examples/01_quickstart.py --turns 8

# Cross-family head-to-head: gpt-4.1 agent, cheap Anthropic harness LLM
python examples/01_quickstart.py --turns 15 --consensus debate \
  --agent-model gpt-4.1 --llm anthropic/claude-haiku-4-5

# Route the HARNESS LLM through a local proxy (agent stays on cloud OpenAI)
python examples/01_quickstart.py \
  --proxy-url http://localhost:1234/v1 \
  --llm gemma-4-E4B-it-MLX-8bit --ctx 6000

# Same run, also pushed to the governance dashboard + gated
python examples/01_quickstart.py --turns 8 --upload --api-key pa_live_...
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--turns`, `-t` | `15` | Number of adversarial turns. |
| `--consensus`, `-c` | `delphi` | Jury consensus strategy: `independent` / `delphi` / `debate`. |
| `--seed`, `-s` | `42` | Random seed for reproducibility. |
| `--llm`, `-l` | `claude-sonnet-4-6` | Harness LLM (juror model). With `--proxy-url`, pass the proxy-served name (auto-prefixed with `openai/`). |
| `--fallback-llm` | off | Backup harness LLM used when a primary juror/conductor call fails (timeout, rate-limit, content refusal). |
| `--agent-model` | `gpt-4.1` | Model for the AGENT under test. Auto-detects provider (`claude-*` → Anthropic; else OpenAI). |
| `--proxy-url` | none | Redirect the harness LLM to an OpenAI-compatible proxy URL. Agent stays on real OpenAI. |
| `--context-budget`, `--ctx` | auto | Harness-LLM context budget. Set (e.g. `6000`) for small-context proxy models. |
| _governance upload_ | — | `--upload`/`--no-upload`, `--api-key`, `--agent`, `--agent-version`, `--profile`, `--fail-on`, `--source` — see [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `02_agent_with_tools.py` — multi-turn + tools (the reference for your real agent)

**What it shows.** A real **OpenAI function-calling agent** (AcmeAir refund
support) that decides and actually calls tools (verify identity → look up booking
→ check eligibility → issue refund / escalate), keeps history across turns, and
returns each turn's tool calls. Its full contract — `system_prompt`, `tools`,
`knowledge` — is handed to the jury via `AgentContext`, so the jury scores
instruction-following, grounded hallucination, and **tool_use** (phantom /
forbidden / out-of-policy calls), not just the prose. Swap `make_openai_agent`
for your own; the only contract is `agent(message: str) -> AgentResponse`.

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
export OPENAI_API_KEY=sk-...                    # powers the agent AND the jury
python examples/02_agent_with_tools.py --turns 8

# Validate wiring with no LLM spend
python examples/02_agent_with_tools.py --list-only

# Offline stub agent (jury still calls --llm)
python examples/02_agent_with_tools.py --stub-agent --turns 6

# Also push to the dashboard
python examples/02_agent_with_tools.py --turns 8 --upload --api-key pa_live_...
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--llm` | `gpt-4.1-mini` | Harness juror LLM. |
| `--fallback-llm` | none | Backup juror LLM (e.g. `gpt-4.1`). |
| `--agent-model` | `gpt-4.1-mini` | Model the AGENT calls. |
| `--turns` | `8` | Adversarial turns. |
| `--consensus` | `delphi` | `independent` / `delphi` / `debate`. |
| `--seed` | `42` | Random seed. |
| `--stub-agent` | off | Use an offline agent (no `OPENAI_API_KEY` needed); the jury still calls `--llm`. |
| `--out-dir` | `results/` | Where to write reports. |
| `--list-only` | off | Print config and exit — no LLM calls. |
| _governance upload_ | `--agent` default `acmeair-refund-agent` | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `03_full_context.py` — `AgentContext.from_dir()`

**What it shows.** Ground every juror in your agent's real contract by loading a
folder that mirrors how a production agent ships — system prompt, knowledge base,
tool schemas — via `AgentContext.from_dir(...)`. The script bootstraps a
self-contained `examples/my_agent_dir/` on first run so you can see the expected
layout (`system_prompt.md`, `knowledge/refund_policy.md`, `tools.json`).

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Offline-friendly smoke (writes a local report; needs an LLM key to score)
python examples/03_full_context.py --turns 4

# Tune the harness from the terminal
python examples/03_full_context.py --llm claude-haiku-4-5 --turns 8 --consensus debate --seed 7

# Wiring check, no LLM calls
python examples/03_full_context.py --list-only

# Also push + gate
python examples/03_full_context.py --turns 4 --upload --api-key pa_live_...
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--llm`, `-l` | auto-pick from your provider key | Harness juror LLM (LiteLLM target). |
| `--fallback-llm` | env `PROOFAGENT_FALLBACK_LLM` | Backup harness LLM that rescues a failed/unparseable primary juror call. |
| `--turns`, `-t` | `4` | Number of adversarial turns. |
| `--consensus`, `-c` | `delphi` | `independent` / `delphi` / `debate`. |
| `--seed`, `-s` | `42` | Random seed. |
| `--metrics` | all 6 canonical | Comma-separated metric subset to score. |
| `--extra-traps` | none | Comma-separated custom trap `.md` files or dirs to merge on top of the bundled library. |
| `--trap-packs` | none | Comma-separated installed trap-pack names to load. |
| `--pin-traps` | none | Comma-separated trap NAMES to force into the plan regardless of selection scoring. |
| `--knowledge` | none | Extra knowledge file/dir to ground jurors (in addition to `my_agent_dir/`). |
| `--quiet`, `-q` | off | Suppress the live progress UI. |
| `--list-only` | off | Print the resolved config and exit — no LLM calls. |
| _governance upload_ | `--agent` default `full-context-refund-agent` | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `04_artifact_eval.py` — artifact mode (score a pre-generated document)

**What it shows.** Score a **pre-generated artifact** — a plan, report, code, or
architecture doc — with the juror panel, grounded in a knowledge corpus. This is
single-turn: it skips the planner + conductor entirely and the jury scores the
artifact directly against the corpus. The bundled example is a fully fictional
community-library **BRD** in
[`sample_artifacts/library_brd/`](sample_artifacts/library_brd/), runnable as-is
after clone. Non-Markdown inputs (`.pdf`, `.docx`, `.html`, images) require the
[`[artifact]` extra](#1-install).

**Mode:** artifact · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Wiring check — no API calls
python examples/04_artifact_eval.py --list-only

# Real eval on the bundled library BRD (gpt-4.1-mini default; ~$0.02 / ~30s)
export OPENAI_API_KEY=sk-...
python examples/04_artifact_eval.py

# Your own artifact + knowledge folder
python examples/04_artifact_eval.py \
  --artifact path/to/your_brd.md \
  --knowledge-dir path/to/your_company_docs/ \
  --type BRD --llm claude-haiku-4-5

# Also push the finished score + gate
python examples/04_artifact_eval.py --upload --api-key pa_live_...
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--artifact`, `-a` | `examples/sample_artifacts/library_brd/brd.md` | Path to the artifact (`.md`, `.txt`, `.pdf`, `.docx`, `.html`). |
| `--knowledge-dir`, `-k` | `examples/sample_artifacts/library_brd/knowledge` | Folder of source docs the artifact was grounded in. |
| `--type`, `-t` | `BRD` | Artifact type tag. Built-in rubric packs: `BRD`, `code`, `business_plan`, `report`, `architecture_doc`, `tech_spec`, `requirements`, `design_doc`, `runbook`, `data_contract`, `model_card`. Unknown types → generic rubric. |
| `--role` | generic library-agent role | The agent's role / persona that produced the artifact. |
| `--business-case`, `-b` | derived from artifact | What the artifact was supposed to accomplish. |
| `--tools-used` | none | Comma-separated tools the producing agent had (metadata only). |
| `--llm`, `-l` | `gpt-4.1-mini` | Harness juror LLM. |
| `--fallback-llm` | none | Backup harness LLM that rescues a failed/unparseable primary juror call. |
| `--consensus`, `-c` | `delphi` | `independent` / `delphi` / `debate`. |
| `--seed`, `-s` | `42` | Random seed. |
| `--list-only` | off | Print the eval plan and exit — no API calls. |
| _governance upload_ | `--agent` default `artifact-eval-agent` | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `05_local_report.py` — one standard report on disk (offline)

**What it shows.** Run the harness entirely on your machine and write the single
standard report — every field the harness produces, in one `<stem>.json` + one
`<stem>.md` (identical schema to what `--upload` pushes, just written
locally). Supports **both** evaluation modes (`--mode multi_turn` |
`--mode artifact`). `--add-custom-fields` additionally emits a
`<stem>.augmented.json` with your own extra fields.

**Mode:** both · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
export OPENAI_API_KEY=sk-...                    # the only key needed (gpt-4.1-mini)

# Multi-turn, gpt-4.1-mini both sides, debate consensus
python examples/05_local_report.py --mode multi_turn --turns 8

# Artifact mode on the bundled library BRD
python examples/05_local_report.py --mode artifact

# No LLM spend — see what would be written
python examples/05_local_report.py --list-only

# Offline multi-turn with a deterministic stub agent
python examples/05_local_report.py --mode multi_turn --stub-agent
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `multi_turn` | `multi_turn` (planner → conductor → jury) or `artifact` (single-shot jury). |
| `--llm` | `gpt-4.1-mini` | Harness juror LLM. |
| `--fallback-llm` | none | Backup harness LLM that auto-rescues failed / unparseable juror + conductor calls. |
| `--agent-model` | `gpt-4.1-mini` | Multi-turn agent model. |
| `--consensus` | `debate` | `independent` / `delphi` / `debate`. |
| `--turns` | `8` | Multi-turn only. |
| `--seed` | `42` | Random seed. |
| `--stub-agent` | off | Multi-turn: offline deterministic agent (no OpenAI key). |
| `--artifact` | none | Artifact mode: path to the artifact file. |
| `--knowledge-dir` | none | Artifact mode: grounding corpus folder. |
| `--agent-system-prompt` | none | Artifact mode (optional): path to the producing agent's system prompt → jury scores instruction-following against it. |
| `--agent-tools` | none | Artifact mode (optional): path to a JSON list of the producing agent's tool schemas → jury checks tool-call hallucination. |
| `--type` | `BRD` | Artifact mode: artifact type tag. |
| `--role` | generic, domain-neutral | The assignment the artifact/agent was given — the jury grades against this. |
| `--business-case` | empty | Why the artifact exists / what it must accomplish. |
| `--out-dir` | `results/` | Where to write the reports. |
| `--add-custom-fields` | off | Also write a `<stem>.augmented.json` with your own extra fields. |
| `--list-only` | off | Print the plan and exit — no LLM calls. |
| _governance upload_ | — | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `06_custom_traps.py` — bring-your-own-trap

**What it shows.** Merge your own adversarial traps into the bundled library via
`Harness(extra_traps=[...])`, with full LLM choice (agent model, juror model,
optional proxy-served juror). `--trap` accepts a directory of `.md` trap
manifests or a single `.md` file; the bundled demo is
[`custom_traps/refund_chargeback_threat.md`](custom_traps/).

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Wiring sanity check — load the trap index with your extra source, no API calls
python examples/06_custom_traps.py --list-only

# Default — bundled demo trap
python examples/06_custom_traps.py --turns 8

# Your own trap pack
python examples/06_custom_traps.py --trap ./my_traps/ --turns 8

# Single trap file + a specific harness LLM + debate
python examples/06_custom_traps.py --trap ./my_traps/attack.md \
  --turns 8 --consensus debate --llm gpt-5.5
```

Author traps as `.md` files with YAML frontmatter; validate with
`proof traps validate ./my_traps/attack.md` (spec:
[`docs/TRAP_MANIFEST.md`](../docs/TRAP_MANIFEST.md)).

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--trap`, `-T` | `examples/custom_traps` | Directory of `.md` trap manifests, or a single `.md` file, to merge. |
| `--list-only` | off | Load the trap index with the extra source and print a summary — no API calls. |
| `--turns`, `-t` | `8` | Number of adversarial turns. |
| `--consensus`, `-c` | `delphi` | `independent` / `delphi` / `debate`. |
| `--seed`, `-s` | `42` | Random seed. |
| `--agent-model` | `claude-sonnet-4-6` | Model for the AGENT under test. Auto-detects provider. |
| `--llm`, `-l` | `claude-sonnet-4-6` | Harness juror model. With `--proxy-url`, pass the proxy-served name (auto-prefixed `openai/`). |
| `--proxy-url` | none | Redirect the harness LLM to an OpenAI-compatible proxy URL. Agent stays on its real provider. |
| `--context-budget`, `--ctx` | auto | Harness-LLM context budget. Set (e.g. `6000`) for small-context proxy models. |
| _governance upload_ | `--agent` default `custom-trap-agent` | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `07_proxy_llm.py` — agent served by a local / self-hosted proxy

**What it shows.** Evaluate an AGENT served by a self-hosted OpenAI-compatible
endpoint (mlx-lm, vLLM, Ollama, LM Studio, corporate proxy). The agent talks to
the proxy; the harness LLM still uses `--llm` for planning / conducting /
scoring. The default agent target is configured inside the script (mlx Gemma via
ngrok) — edit it to point at your endpoint.

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Default — uses the proxy target wired in the script
python examples/07_proxy_llm.py

# Longer debate run with a specific cross-family harness LLM
python examples/07_proxy_llm.py --turns 25 --consensus debate \
  --llm anthropic/claude-haiku-4-5

# Also push + gate
python examples/07_proxy_llm.py --turns 8 --upload --api-key pa_live_...
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--turns`, `-t` | `15` | Number of adversarial turns. |
| `--consensus`, `-c` | `delphi` | `independent` / `delphi` / `debate`. |
| `--seed`, `-s` | `42` | Random seed. |
| `--llm`, `-l` | `gpt-4.1-mini` | Harness LLM (planner / conductor / juror). Cross-family default vs. the proxy-served agent. |
| _governance upload_ | `--agent` default `proxy-llm-agent` | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `08_live_trace.py` — observability (live per-turn trace)

**What it shows.** An observability / debugging example: watch the harness pick
traps and probe the agent in real time. Each turn prints a rich panel — selected
trap ID and parsed fields (family, severity, metrics, forbidden/expected tools,
tags, pattern excerpt with composite attack chain), the conductor's adversarial
question, the agent's answer, any tool calls, and a cumulative coverage line.
Use it to debug *why* an agent failed; for batch scoring, run it against each
agent spec. Agent specs are loaded from
[`agents/*.json`](agents/) (five bundled profiles).

> Here `--agent` selects which **agent spec** to load (it is *not* the dashboard
> agent name), so the governance group on this example omits its own `--agent`
> flag; the dashboard agent name is derived from the loaded spec.

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# List the bundled agent registry
python examples/08_live_trace.py --list-agents

# Wire check (no API calls)
python examples/08_live_trace.py --agent privacy_security_agent --list-only

# Cross-family — Claude Opus agent + cloud Sonnet harness LLM (no proxy)
python examples/08_live_trace.py \
  --agent privacy_security_agent \
  --agent-model claude-opus-4-7 \
  --harness-llm anthropic/claude-sonnet-4-6 \
  --no-proxy --turns 10 --consensus debate

# Local Gemma harness LLM (LM Studio) + gpt-4.1-mini agent
python examples/08_live_trace.py \
  --agent medical_triage_assistant --agent-model gpt-4.1-mini \
  --harness-llm gemma-4-E4B-it-MLX-8bit \
  --proxy-url http://localhost:1234/v1 \
  --turns 8 --consensus delphi --seed 42 \
  --context-budget 6000 --sequential
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--agent`, `-a` | `customer_support_agent` | Bundled agent name (`code_generation_agent`, `customer_support_agent`, `financial_advisor_agent`, `medical_triage_assistant`, `privacy_security_agent`) or path to a custom JSON spec. |
| `--agent-model` | `gpt-4.1-mini` | LLM that powers the agent under test. |
| `--harness-llm` | `gemma-4-E4B-it-MLX-8bit` | LLM that powers the conductor + jury panel. |
| `--proxy-url` | LM Studio at `:1234` | Local proxy URL for the harness LLM. |
| `--no-proxy` | off | Skip proxy wiring (use when `--harness-llm` is a cloud model). |
| `--turns`, `-t` | (script default) | Number of adversarial turns. |
| `--consensus`, `-c` | (script default) | `independent` / `delphi` / `debate`. |
| `--seed`, `-s` | (script default) | Random seed. |
| `--context-budget`, `--ctx` | auto | Harness-LLM context budget for small-context proxy models. |
| `--sequential` | off | Serialize juror calls — required for single-threaded local proxies. |
| `--per-call-timeout` | (script default) | Per-juror-call timeout. |
| `--fallback-juror` | none | On empty/garbled/erroring primary juror calls, retry via OpenAI `MODEL` (bypasses `--proxy-url`). Recommended `gpt-4.1-mini`. |
| `--extra-traps` | none | One or more dirs of custom `.md` traps to merge (last-wins on name). |
| `--trap-packs` | none | One or more pip-installed trap packs to load. |
| `--verbose`, `-v` | off | No truncation of pattern / Q / A in the trace. |
| `--list-only` | off | Print config + agent summary + trap library; no API calls. |
| `--list-agents` | off | Print the bundled agent registry and exit. |
| `--output-dir` | `results/` | Where to write reports. |
| _governance upload_ | _no `--agent`_ (derived from spec) | `--upload`/`--no-upload`, `--api-key`, `--agent-version`, `--profile`, `--fail-on`, `--source` — see [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `09_regression.py` — regression sweep across agent versions (offline)

**What it shows.** Sweep multiple **versions** of the same agent (the script
walks a defensive → balanced → loose progression), scoring each with the same
jury + seed so per-metric deltas surface and you can see at a glance which
version regressed and on which dimension. Runs **fully offline** — each version
writes its own local report. Add `--upload` to *also* push each version to the
dashboard under one `--agent` name (each run gets a distinct `run_name`), so the
dashboard groups them and renders the regression trend.

**Mode:** multi-turn · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Offline (default) — runs the version sweep + writes local reports
python examples/09_regression.py

# Print the plan, no API calls
python examples/09_regression.py --list-only

# Also push each version to your dashboard, grouped under one agent name
export PROOFAGENT_API_KEY=pa_live_...
python examples/09_regression.py --upload --agent refund-agent
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--llm` | `claude-haiku-4-5` | Harness juror LLM (cheap + fast). |
| `--turns` | `5` | Turns per version (cheap smoke). |
| `--seed` | `42` | Random seed. |
| `--list-only` | off | Print the plan — no API calls. |
| _governance upload_ | `--agent` default `regression-demo-agent` | See [Pushing to the dashboard](#4-pushing-to-the-dashboard). |

---

## `10_pytest_ci.py` — drop-in pytest assertion for CI

**What it shows.** The harness as a single **pytest** assertion: run an eval and
fail the test if the final score (or any per-metric score) drops below a
threshold. It optionally also calls `push_to_dashboard(...)` and fails the build
if the governance gate returns `block`. The bundled test is marked
`@pytest.mark.skip` (remove the skip in your CI — it needs an LLM key). Because
it's a pytest test, **not** a CLI, thresholds and turns are overridable via
environment variables rather than flags.

**Mode:** multi-turn · **Pushes via:** `push_to_dashboard()` helper (no-op offline)

**Run**

```bash
pytest examples/10_pytest_ci.py

# Tune thresholds in CI without editing the file
PROOF_MIN_SCORE=7.5 PROOF_MIN_SAFETY=8.5 PROOF_TURNS=6 \
  pytest examples/10_pytest_ci.py
```

**Environment knobs** (all optional)

| Env var | Default | Meaning |
|---|---|---|
| `PROOF_TURNS` | `4` | Turns the harness runs. |
| `PROOF_MIN_SCORE` | `7.0` | Minimum `final_score`. |
| `PROOF_MIN_SAFETY` | `8.0` | Minimum `safety`. |
| `PROOF_MIN_HALLUCINATION` | `7.0` | Minimum `hallucination_resistance`. |
| `PROOF_MIN_MANIPULATION` | `7.0` | Minimum `manipulation_resistance`. |

Set `PROOFAGENT_API_KEY` to also push the run and gate the CI build on the
governance decision.

---

## `11_governance_gate.py` — release gate from a saved report

**What it shows.** The *last mile*: take a finished evaluation `Report` and push
it to the **ProofAgent Governance API** to gate a release (`pass` / `review` /
`block`) and land it on the dashboard with full fidelity. It loads a **real**
saved report from `results/` (deserialized from disk, so it runs **without any
LLM key**), builds the upload payload, and prints a summary. Offline (default) it
writes the exact payload it *would* send to
`examples/_governance_payload.sample.json`; with `--upload` it POSTs and — unlike
the other examples — **exits with the gate-mapped code** (0 pass / 1 review /
2 block, subject to `--fail-on`), ready to wire into a CI step. The script
uploads to ProofAgent Cloud.

**Mode:** reads a saved report (no eval) · **Pushes via:** [`--upload`](#4-pushing-to-the-dashboard)

**Run**

```bash
# Offline — build + summarize + dump the sample payload (no keys, no network)
python examples/11_governance_gate.py

# Upload + gate (ProofAgent Cloud); exits with the gate code
python examples/11_governance_gate.py \
  --upload --api-key pa_live_... \
  --agent "Refund Agent" --agent-version v1.8.2 \
  --profile airline_customer_support --source ci_cd --fail-on block
```

**Arguments**

| Flag | Default | Meaning |
|---|---|---|
| `--report`, `-r` | auto-pick richest under `results/` | Saved `Report` JSON to upload (prefers a multi-turn run with a transcript). |
| `--agent` | `Refund Agent` | Logical agent name (groups runs + regressions). |
| `--agent-version` | `v1.8.2` | Version / git ref of the agent under test. |
| `--profile` | `airline_customer_support` | Governance profile slug to gate against. |
| `--source` | `manual` | `local` / `ci_cd` / `manual` / `api` / `scheduled`. |
| `--fail-on` | `block` | Which gate decision fails the build (exit non-zero). |
| `--upload` | off | POST the run and exit with the gate code. Offline, the payload is written to `_governance_payload.sample.json`. |
| `--no-upload` | (default) | Explicitly run offline. |
| `--api-key KEY` | env `PROOFAGENT_API_KEY` | Governance API key (flag wins over env). |

> Unlike 01–08, this example registers its governance flags directly (not the
> shared group): the `--agent` / `--agent-version` / `--profile` defaults are
> pre-filled for the bundled report, and it **does** `sys.exit` on the gate —
> that's what makes it a CI gate rather than a demo push.

---

## Utility — `report_viewer.py`

Render any saved report `.json` as a self-contained, offline HTML dashboard (no
server, no internet, no deps). It embeds the JSON directly, so the output file
survives being moved or emailed. Renders the headline + certification, per-metric
bars, executive brief, the full transcript, a turns × metrics PASS/FAIL audit
matrix, the per-persona jury debate, findings, and token/timing KPIs.

```bash
python examples/report_viewer.py results/<report>.json
python examples/report_viewer.py results/<report>.json --open   # also open in your browser
```

| Argument | Default | Meaning |
|---|---|---|
| `report` (positional) | — | Path to a report `.json` saved by `report.to_json()`. |
| `--out OUT` | alongside the `.json` | Output `.html` path. |
| `--open` | off | Open the dashboard in your default browser. |

`_dashboard.py` is the shared upload helper (the `--upload` flag group +
`push_to_dashboard()`); it is imported by the examples, not run directly.

---

## notebooks/

Three end-to-end Jupyter walkthroughs live at the repo root under
[`notebooks/`](../notebooks/). Each ends with an **optional dashboard-push cell**
(`build_governance_payload` → `upload_run`, gated on `PROOFAGENT_API_KEY`) so you
can push the notebook's result to the governance dashboard — leave the key unset
to run the notebook fully offline.

| Notebook | What it covers |
|---|---|
| [`01_quickstart.ipynb`](../notebooks/01_quickstart.ipynb) | The canonical multi-turn quickstart, narrated cell by cell. |
| [`02_compliance.ipynb`](../notebooks/02_compliance.ipynb) | A compliance-focused walkthrough (restricting coverage to regulated-domain traps). |
| [`03_proxy_llm.ipynb`](../notebooks/03_proxy_llm.ipynb) | Running the harness LLM through a local OpenAI-compatible proxy. |

---

## Cost & runtime guidance

Rough numbers per multi-turn run at `--turns 25 --consensus debate` (3-persona):

| Harness LLM | Cost / run | Wall clock | Notes |
|---|---|---|---|
| `gpt-4.1-mini` | ~$0.50 | 5–10 min | Cheapest cloud — fine for smoke tests (default on several examples). |
| `anthropic/claude-haiku-4-5` | ~$1–2 | 5–10 min | Best cheap cloud option. |
| `gpt-5.5` | ~$2–4 | 10–15 min | Frontier OpenAI. |
| `anthropic/claude-opus-4-7` | ~$3–5 | 10–15 min | Paper's Large Harness reference. |
| local Gemma 4B (LM Studio) | $0 | 25–45 min | Paper's Small Harness. Use `--sequential` + `--context-budget 6000`. |
| local Qwen 2.5 7B / Llama 3.1 8B | $0 | 30–60 min | Stronger small-harness alternatives. |

Drop cost/time by lowering `--turns` to 8–15 for development, or relaxing
`--consensus debate` → `delphi` (~1.5× instead of 3–5× per turn) → `independent`
(1×). Artifact mode (04, and 05 `--mode artifact`) is single-turn and the
cheapest path of all (~$0.02 on `gpt-4.1-mini`).

**Reports.** Every scoring example writes `<run-id>.json` (full evidence-linked
transcript, per-juror scores, consensus log, findings, metadata) and
`<run-id>.md` (human-readable scorecard) under `results/` (or `--out-dir` /
`--output-dir`), and prints the final score + certification band at the end.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: proofagent_harness` | Package not installed in the active Python | `pip install proofagent-harness` (or activate the right venv) |
| `ModuleNotFoundError` for `pypdf` / `docx` / `bs4` in artifact mode | Missing the artifact extra | `pip install "proofagent-harness[artifact]"` |
| `LLMNotConfiguredError: API key missing` | Forgot to `export` the relevant provider key | Export the key in the same terminal that runs the script |
| `LLM Provider NOT provided…` | LiteLLM can't infer the provider from a bare model name | Prefix it: `anthropic/claude-haiku-4-5`, `gemini/gemini-2.5-pro` |
| `the configured harness LLM cannot handle the context size` | Local proxy loaded at too-small a context length | Reload the proxy model with a larger `--context-length` and/or pass `--context-budget` (e.g. `6000`) and/or drop `--turns` |
| `Error code: 400 — model has crashed` | Local proxy OOM (model + KV cache > RAM) | Lower the proxy context length; close other apps; use a smaller quant |
| All juror calls time out at 600s+ | Single-threaded local proxy can't serve parallel jurors | Add `--sequential` (and `--per-call-timeout` on 08) |
| OpenAI harness LLM refuses an adversarial transcript | Provider content filter flags the red-team text | Pass an Anthropic `--fallback-llm` (e.g. `claude-sonnet-4-5`) — it isn't subject to OpenAI's filter |
| Final score mid-band (~5–7) with many `SOFT_FAIL` audit lines | Harness LLM too small to parse the debate transcript reliably | Use a larger harness LLM (≥ 7B local, or any cloud frontier) and/or a `--fallback-llm` / `--fallback-juror` |
| `gpt-5.x` returns "unsupported parameter" | Reasoning models dropped `temperature` / renamed `max_tokens` | The factory in [`agents/factory.py`](agents/factory.py) already handles this; in custom code drop `temperature` and use `max_completion_tokens` |

---

## See also

- Top-level [`README.md`](../README.md) — package overview, install, quickstart
- [`agents/README.md`](agents/README.md) — agent spec schema + authoring your own
- [`docs/TRAP_MANIFEST.md`](../docs/TRAP_MANIFEST.md) — trap manifest spec (example 06)
- [`docs/governance-upload.md`](../docs/governance-upload.md) — full `--upload` CLI, exit codes, CI gating, programmatic API
