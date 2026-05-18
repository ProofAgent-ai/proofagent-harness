# proofagent-harness — Documentation

Welcome. This folder contains the full reference for `proofagent-harness`. The repository root [`README.md`](../README.md) is the 30-second pitch; this is where the depth lives.

If you're new, follow the **Get Started** path top-to-bottom. If you've already shipped your first eval, jump to whichever capability or red-team workflow you need.

---

## Get Started

| | |
|---|---|
| 📦 [Install](install.md)         | Install the package, set up your model |
| ⚡ [Quickstart](quickstart.md)    | 10 lines of code, your first evaluation in under a minute |
| 💡 [Why proofagent-harness](why.md) | What this library does that single-shot eval libs don't |

## Capabilities

| | |
|---|---|
| 🏗️ [How it works](how-it-works.md)        | The 5-agent pipeline (planner → conductor → jury → consensus → reporter) |
| 📐 [The 5 metrics](metrics.md)             | What gets scored, certification tiers, scoring policy |
| 🧩 [Your agent + AgentContext](your-agent.md) | Plain function, closure, or `AgentResponse`; feeding in system prompt + knowledge + tools |
| 🧪 [CI integration](ci-integration.md)     | pytest assertion pattern + recommended thresholds |
| 💻 [CLI + Recipes](cli.md)                  | `proof run` / `proof traps` / `proof metrics` + smoke/production/regulated recipes |
| ⚙️ [Configuration](configuration.md)       | All `Harness(...)` knobs in one place |
| 🎲 [Reproducibility](reproducibility.md)   | Seeds, temperature, variance, stability runs |

## Red Teaming

| | |
|---|---|
| 🎯 [Traps & skills overview](traps.md)           | What traps are, the 11 bundled families, where skills fit in |
| 📜 [Trap manifest v1.0 (spec)](TRAP_MANIFEST.md) | The canonical `.md` format for every trap — frontmatter, sections, vocabularies |
| 🔧 [Bring your own traps](red-teaming.md)         | Author → validate → normalize → run, end-to-end |

## Reference

| | |
|---|---|
| 🆚 [Open source vs Hosted Platform](platform-vs-hosted.md) | When to stay on OSS, when to scale through ProofAgent Platform |
| ❓ [FAQ](faq.md)                                            | Most-asked questions, with code |

## Beyond the docs

- 📂 [`examples/`](../examples/) — 8 runnable Python examples (quickstart, pytest, custom traps, proxy juror, …)
- 📓 [`notebooks/`](../notebooks/) — 4 end-to-end Jupyter walkthroughs (local + Colab + compliance + proxy LLM)
- 🤝 [`CONTRIBUTING.md`](../CONTRIBUTING.md) — how to contribute a trap, a persona, or a code PR
- ⚖️ [`LICENSE`](../LICENSE) — Apache 2.0
- 📜 [`NOTICE`](../NOTICE) — attribution requirements
- 🏷️ [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) — runtime dependencies
