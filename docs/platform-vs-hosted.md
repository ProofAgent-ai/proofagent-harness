# Open source vs Hosted Platform

[← back to docs](README.md)

`proofagent-harness` is the local OSS test harness. The [ProofAgent](https://proofagent.ai) hosted Platform is the enterprise SaaS layer built on top of it. Both speak the same vocabulary (same metrics, same trap families, same readiness tiers).

## Comparison

| | OSS Harness *(this repo)* | Hosted Platform |
|---|:---:|:---:|
| Multi-turn adversarial evaluation + jury consensus | ✓ | ✓ |
| Bring-your-own LLM (Anthropic / OpenAI / Gemini / Ollama / vLLM) | ✓ | ✓ |
| 64 bundled traps across 11 families | ✓ | ✓ |
| Domain-aware trap selection | ✓ | ✓ |
| Local + CI execution | ✓ | ✓ |
| `pytest` integration | ✓ | — |
| **Hosted dashboards + run history** | — | ✓ |
| **Production log replay** — back-test real conversations against the same scoring framework | — | ✓ |
| **Artifact grading** — reports, plans, code, decisions evaluated against structured rubrics | — | ✓ |
| **Multi-agent trace scoring** — routers, sub-agents, retrievers, tools as one connected system | — | ✓ |
| **Expert human review workflows** — clinical / legal / finance / security domain reviewers | — | ✓ |
| **Team RBAC + audit logs** | — | ✓ |
| **Readiness reports + deployment evidence** for internal stakeholders | — | ✓ |
| **Regression monitoring** across releases, model updates, prompt changes | — | ✓ |
| **Proprietary domain MCP + premium trap packs** (healthcare / finance / legal / HR / regulated) | — | ✓ |
| **Air-gapped enterprise deployment** | — | ✓ |

## When to use each

### Use the **OSS Harness** when

- You're a developer iterating on a single agent in your terminal or IDE
- You want gateable pre-commit / CI evaluations with `pytest`
- You're prototyping a new agent and need rapid feedback
- You're authoring + testing custom traps for a domain
- Your team is small (one to a few people) and async dashboards aren't worth the setup
- You need full local control — air-gapped runs with `Ollama` / `vLLM`

### Move to the **hosted Platform** when

- Multiple teams need to evaluate, review, and govern agents
- You need **production log replay** to back-test real conversations against the scoring framework
- You need to track **readiness regressions across releases** in a dashboard, not a JSON dump
- You need **expert human review** for high-stakes / regulated launches
- You're shipping **multi-agent systems** (router + sub-agents + retrievers + tools) and want orchestration-level scoring
- You need **audit-grade reports** for internal stakeholders, partners, or regulators
- You want access to **proprietary domain MCP** + premium trap packs that aren't in OSS

## How they fit together

```
DEVELOPMENT             PRE-PRODUCTION              PRODUCTION
─────────────           ──────────────              ──────────
proofagent-harness  →   proofagent-harness    →     ProofAgent Platform
local + CI              + Platform release-gate     dashboards · log audits
                                                    expert review · regression tracking
```

The same `Trap` definitions and `Report` schema flow through both — start on OSS, scale to Platform when you outgrow the local + CI loop without rewriting tests.

## Where to learn more

- 🌐 [proofagent.ai/platform](https://proofagent.ai/platform) — full Platform walkthrough
- 📞 [Book a demo](https://proofagent.ai) — see hosted dashboards + log replay + expert review live
- 🆓 Keep using this OSS Harness regardless — it's Apache 2.0 and always will be
