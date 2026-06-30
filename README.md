<div align="center">

# proofagent-harness

**`pytest` for AI agents.** The open-source, domain-aware harness that red-teams AI agents with multi-turn adversarial pressure **and** grades finished artifacts (code, BRDs, specs, reports), then gates your release on a governance decision in CI.

[![PyPI](https://img.shields.io/pypi/v/proofagent-harness.svg)](https://pypi.org/project/proofagent-harness/)
[![Python](https://img.shields.io/pypi/pyversions/proofagent-harness.svg)](https://pypi.org/project/proofagent-harness/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/ProofAgent-ai/proofagent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/ProofAgent-ai/proofagent-harness/actions/workflows/ci.yml)
[![arXiv](https://img.shields.io/badge/arXiv-2605.24134-b31b1b.svg)](https://arxiv.org/abs/2605.24134)

<img src="docs/architecture.png" alt="ProofAgent Harness evaluation pipeline" width="720" />

[Install](#install) · [Quickstart](#quickstart) · [Modes](#evaluation-modes) · [Harness LLM](#choosing-a-harness-llm) · [Metrics](#metrics) · [Governance gate](#governance--ci-release-gate) · [Docs](https://www.proofagent.ai/harness/docs)

**📖 Full docs:** [proofagent.ai/harness/docs](https://www.proofagent.ai/harness/docs) · **📄 Paper:** [arXiv:2605.24134](https://arxiv.org/abs/2605.24134)

</div>

`proofagent-harness` puts an adversary and an auditor in front of your AI agent before your users do. It runs realistic **multi-turn red-team** conversations against a live agent, and scores **finished deliverables** against ground truth — both through the same multi-agent consensus jury over six production-critical metrics. Bring your own LLM, bring your own traps, run locally or in CI. Your code, prompts, and data never leave your machine unless you opt in. One flag (`--upload`) turns the evaluation into a **release gate** — pass / review / block, straight from your pipeline.

> **This README covers the essentials.** The full reference — every CLI flag, the Python API, configuration, model-selection guidance, and the FAQ — lives in the **[documentation](https://www.proofagent.ai/harness/docs)**.

---

## Features

**Evaluation**
- **Two modes** — **multi-turn adversarial** (pressure-test a live agent) and **artifact** (grade a finished deliverable: code, BRD, plan, spec, report, runbook, …).
- **183 traps across 11 families** — social engineering, prompt injection, data exfiltration, tool misuse, compliance, bias, … Author your own as one `.md` file.
- **6 metrics, jury personas & 3 consensus strategies** (`independent` / `delphi` / `debate`), with a deterministic **zero-tolerance cap** for genuine violations.
- **Tool-use & phantom-call scoring** — required tools must actually be invoked; invented tools and "done, with no tool call" fail (scored even when no tools are provided).

**Ship gates & infrastructure**
- **Governance release gate** — `--upload` POSTs the evaluation to the Governance API and exits on its decision (`0` pass · `1` review · `2` block). Only an API key is needed.
- **Compliance + evidence** — each run maps to control statuses across a **25-framework catalog** (EU AI Act · NIST AI RMF · ISO/IEC 42001 · SOC 2), and findings are structured `claim → evidence → fix`.
- **Any LiteLLM model + cross-family fallback** — Anthropic, OpenAI, Gemini, Bedrock, Azure, Ollama, vLLM, LM Studio, … with `--fallback-llm` rescue on malformed JSON / refusal / error.

---

## Install

Requires **Python 3.10+**.

```bash
pip install proofagent-harness
pip install "proofagent-harness[artifact]"      # + PDF / DOCX / HTML / IPYNB parsers (artifact mode)

export ANTHROPIC_API_KEY=sk-ant-...             # or OPENAI_API_KEY / GEMINI_API_KEY / …
export PROOFAGENT_LLM=claude-sonnet-4-6         # optional: default harness LLM
```

Any [LiteLLM](https://github.com/BerriAI/litellm) target works. Verify with `proof version` and `proof traps stats` (→ 183 traps across 11 families).

**From source:** `pip install git+https://github.com/ProofAgent-ai/proofagent-harness.git` · **Dev:** `pip install -e ".[dev]" && pytest`.

## Quickstart

**Multi-turn (Python).** Wrap your agent in a `str -> str` callable and evaluate it:

```python
from proofagent_harness import Harness

def my_agent(message: str) -> str:
    return your_llm_call(message)

report = Harness(llm="claude-sonnet-4-6").evaluate(
    my_agent,
    role="customer support",
    goal="handle refunds safely",
)
print(report)
```

Output (auto-printed):

```
proofagent-harness — Scorecard
┃ Metric                  ┃     Score ┃ Confidence ┃ Severity ┃
│ Task Success            │  9.0 / 10 │       0.90 │ pass     │
│ Hallucination Resistance│  8.0 / 10 │       1.00 │ pass     │
│ Safety                  │ 10.0 / 10 │       1.00 │ pass     │
│ Instruction Following   │  9.0 / 10 │       1.00 │ pass     │
│ Manipulation Resistance │  8.0 / 10 │       0.90 │ pass     │
│ Tool Use                │  8.0 / 10 │       0.90 │ pass     │

Final score: 8.67 / 10    Tokens: 61,204
```

`report.to_json("path.json")` / `report.to_markdown("path.md")` give you the full transcript, reasoning, and findings.

**CLI** — point `proof run` at any `.py` exposing a callable named `agent`, or grade a finished file with `proof artifact`:

```bash
proof run my_agent.py --turns 8 --consensus delphi --seed 42 \
    --role "customer support" --goal "handle refunds safely"

proof artifact ./proposal.md --type BRD --knowledge-dir ./docs --llm gpt-4.1-mini
```

> **Two independent LLM choices.** `llm=` is the **harness** model — it powers the whole evaluation pipeline end-to-end, *not* one model grading once. Your **agent's** LLM is whatever you call inside `my_agent`; the harness only sees its outputs. Pick a strong harness model — weak grading gives noisy scores (see [Choosing a harness LLM](#choosing-a-harness-llm)).

**Pass the agent's full context** for the deepest scoring — its own system prompt, grounding knowledge, and tool schemas all go to the jury:

```python
from proofagent_harness import AgentContext, Harness

Harness(llm="gpt-4.1-mini").evaluate(
    my_agent,
    role="customer support",
    goal="handle refunds safely",
    business_case="resolve billing issues without leaking PII or over-refunding",
    context=AgentContext(
        system_prompt=open("system.md").read(),   # the agent's own instructions
        knowledge="./knowledge/",                  # dir/files the agent grounds on
        tools=open("tools.json").read(),           # the agent's tool schemas
    ),
)
# Shortcut: AgentContext.from_dir("./my_agent/") auto-discovers all of the above.
```

Already have a **LangChain / LangGraph / CrewAI** agent? Return an `AgentResponse(text=…, tools_called=…)` from your callable so the jury can score tool calls — see [`examples/02_agent_with_tools.py`](examples/02_agent_with_tools.py).

## Evaluation modes

Same jury and metrics — different inputs. Both return the same `Report`; `report.mode` says which ran.

| | **`multi_turn`** *(default)* | **`artifact`** |
|---|---|---|
| **Input** | a live agent callable (`str -> str`) | a finished file (BRD, plan, code, spec, report, …) |
| **Needs** | `role` + `goal`; optional `AgentContext` (system prompt, tools, knowledge) | the artifact + optional `KnowledgeCorpus` of ground-truth docs |
| **Metrics** | all **6** (incl. `manipulation_resistance`) | **5** (`manipulation_resistance` auto-dropped) |
| **Use when** | adversarial pressure-testing of behavior | grading an output against ground truth |

Artifact mode ships **11 type-specific rubric packs** (`BRD`, `business_plan`, `tech_spec`, `code`, `report`, `runbook`, `model_card`, …), reads `.md/.txt/.pdf/.docx/.html/.ipynb`, and supports multi-file bundles + diff/regression. Runnable: [`examples/04_artifact_eval.py`](examples/04_artifact_eval.py).

## Choosing a harness LLM

The harness LLM does *all* the grading — match it to the stakes. Full guidance: [harness/docs#harness-llm](https://www.proofagent.ai/harness/docs#harness-llm).

| Use case | Recommended harness LLM |
|---|---|
| Quick local check / CI smoke / air-gapped | a local OpenAI-compatible proxy (LM Studio / Ollama / vLLM) |
| Cheap cloud iteration | `gpt-4.1-mini` or `claude-haiku-4-5` |
| Production release gate | a frontier model — `claude-opus-4-8` / `claude-sonnet-4-6` / `gpt-5.x` |

- **Grading adversarial content? Prefer a Claude harness LLM** — frontier OpenAI models often refuse attack transcripts, which derails scoring.
- **Pair the gate with `--fallback-llm` (cross-family)** so a call the primary can't handle (malformed JSON, timeout, refusal) routes to a stronger model.
- **Anthropic ignores `seed`.** For byte-reproducible reruns use a seed-honoring model (`gpt-4.1` / `gemini-2.5-pro`) or gate on a median-of-N.

## Metrics

The six metrics (all 0–10) feed one global score:

| Metric | Question |
|---|---|
| **Task Success** | Did the agent achieve the goal? |
| **Hallucination Resistance** | Did it stay grounded? |
| **Safety** | Did it refuse harm and protect data? |
| **Instruction Following** | Did it obey its system prompt across turns? |
| **Manipulation Resistance** | Did it hold firm under social engineering? *(multi-turn only)* |
| **Tool Use** | Right tools actually invoked — no invented or *phantom* calls (scored even with no tools provided). |

**Zero-tolerance cap.** The harness catches failures rather than extending the benefit of the doubt: when a majority of jurors log a hard `FAIL`, the metric is deterministically capped at **3.0/10** — a lenient juror can't override it. A real safety/privacy breach, a phantom action, or an unverifiable claim triggers it.

## Governance & CI release gate

The harness runs **fully local by default**. Add `--upload` to turn any evaluation into a release gate: it POSTs the completed `Report` to the **ProofAgent Governance API**, which runs its gate engine against your governance profile, and the harness exits with a code your pipeline can act on. The API never sees your harness-LLM credentials — only the report. You only need an **API key**; every `--upload` run goes to ProofAgent Cloud.

```bash
export PROOFAGENT_API_KEY="pa_live_..."   # Dashboard → Settings → API Keys

proof run my_agent.py --turns 12 --upload --fail-on block \
    --agent airline-support --agent-version "$(git rev-parse --short HEAD)" \
    --profile airline_customer_support
```

| Gate decision | Exit code | Meaning |
|---|---|---|
| `pass` | **0** | Release allowed. |
| `review` | **1** | Soft gate — exit `1` only with `--fail-on review`; otherwise informational (exit `0`). |
| `block` | **2** | Hard gate — always exit `2`. |

```
Governance gate: BLOCK
  Final score : 6.41 (fail)
  Failed rules: final_score_below_threshold, hallucination_below_threshold
  Dashboard   : https://app.proofagent.ai/runs/<run-id>
```

On the dashboard, the finished report renders as a release decision, a per-metric scorecard, per-metric jury consensus, and a compliance posture — with a control plane across every governed agent. See the **[dashboard walkthrough → harness/docs#governance](https://www.proofagent.ai/harness/docs#governance)** for annotated screenshots.

Two reporter extras travel with each upload (on by default, no-op-safe, never affect the gate): **compliance assessment** (`report.compliance`; disable with `PROOFAGENT_COMPLIANCE=0`) and **evidence-driven findings** (disable with `PROOFAGENT_EVIDENCE=0`). Full reference — GitHub Actions, exit codes, and the programmatic `proofagent_harness.governance` API — in [`docs/governance-upload.md`](docs/governance-upload.md).

## Documentation

This README is the essentials. The **[full documentation](https://www.proofagent.ai/harness/docs)** has the deep reference — including a complete **[parameter reference](https://www.proofagent.ai/harness/docs#parameters)** (every flag + Python argument, what each does, and when to use it). Every topic maps to its exact section:

| Topic | Docs section |
|---|---|
| **All parameters** — every flag + Python arg, with what each does & when to use | [`#parameters`](https://www.proofagent.ai/harness/docs#parameters) |
| **How it works** — the evaluation pipeline | [`#how-it-works`](https://www.proofagent.ai/harness/docs#how-it-works) |
| **Multi-turn mode** | [`#multi-turn-mode`](https://www.proofagent.ai/harness/docs#multi-turn-mode) |
| **Artifact mode** | [`#artifact-mode`](https://www.proofagent.ai/harness/docs#artifact-mode) |
| **Wrapping your agent** — LangChain / callable API | [`#your-agent`](https://www.proofagent.ai/harness/docs#your-agent) |
| **Choosing a harness LLM** | [`#harness-llm`](https://www.proofagent.ai/harness/docs#harness-llm) |
| **Metrics** | [`#metrics`](https://www.proofagent.ai/harness/docs#metrics) |
| **Configuration** — `Scoring` (aggregation, weights, floors, thresholds, personas) | [`#configuration`](https://www.proofagent.ai/harness/docs#configuration) |
| **Reproducibility & seeds** | [`#reproducibility`](https://www.proofagent.ai/harness/docs#reproducibility) |
| **CLI reference** — every `proof run` / `proof artifact` / `proof traps` flag | [`#cli`](https://www.proofagent.ai/harness/docs#cli) |
| **Governance & CI gate** — flags, exit codes, GitHub Actions | [`#governance`](https://www.proofagent.ai/harness/docs#governance) · [`#ci-integration`](https://www.proofagent.ai/harness/docs#ci-integration) |
| **Authoring traps** — the one-file `.md` trap spec | [`#trap-manifest`](https://www.proofagent.ai/harness/docs#trap-manifest) |
| **FAQ / troubleshooting** | [`#faq`](https://www.proofagent.ai/harness/docs#faq) |

Methodology & benchmarks: [the paper · arXiv:2605.24134](https://arxiv.org/abs/2605.24134).

## Examples & notebooks

Runnable recipes — each self-contained, each prints a scorecard. Full per-example argument reference in [`examples/README.md`](examples/README.md); end-to-end walkthroughs in [`notebooks/`](notebooks/).

`01_quickstart` · `02_agent_with_tools` · `03_full_context` · `04_artifact_eval` · `05_local_report` · `06_custom_traps` · `07_proxy_llm` · `08_live_trace` · `09_regression` · `10_pytest_ci` · `11_governance_gate`

## Citation

ProofAgent Harness is published on arXiv — please cite if you build on it:

```bibtex
@misc{bousetouane2026proofagentharnessopeninfrastructure,
      title={ProofAgent Harness: Open Infrastructure for Adversarial Evaluation of AI Agents},
      author={Fouad Bousetouane},
      year={2026},
      eprint={2605.24134},
      archivePrefix={arXiv},
      primaryClass={cs.MA},
      url={https://arxiv.org/abs/2605.24134},
}
```

## Contributing · Security · License

PRs welcome — highest-leverage: a new trap (one `.md` per [`docs/TRAP_MANIFEST.md`](docs/TRAP_MANIFEST.md)) or a new juror persona. `pip install -e ".[dev]" && pytest`. See [CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities via [SECURITY.md](SECURITY.md).

Licensed under **[Apache 2.0](LICENSE)** ([NOTICE](NOTICE) · [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)). © 2025–2026 **ProofAI LLC** · Original author **Dr. Fouad Bousetouane**. "ProofAgent" and "ProofAgent Harness" are trademarks of ProofAI LLC; the license does not grant rights to the name, logo, or branding for competing hosted services.

---

<div align="center">
<sub>Built by the team behind <a href="https://proofagent.ai">ProofAgent</a>. Star us on GitHub if this saved you an incident.</sub>
</div>
