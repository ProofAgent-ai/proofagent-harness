# Why proofagent-harness

[← back to docs](README.md)

## The problem

Most AI eval libraries score the **last response** with **one judge** against a **fixed test set**. Production agents fail differently:

- in the **third turn**, under social-engineering pressure, when the system prompt has drifted out of context
- via **domain-specific** failure modes (HIPAA leaks, PCI handling, SOX bypass, malware generation) that generic test sets miss
- through **callbacks and follow-ups** an attacker uses to weaponize an earlier concession
- as a **regression** that only shows up when you swap a model, change a prompt, or add a tool

Single-shot + single-judge testing doesn't catch any of that.

## What this harness does differently

| | proofagent-harness | typical eval libs |
|---|:---:|:---:|
| **Domain-aware planning** — picks HIPAA traps for healthcare, PCI for retail, malware-gen probes for code agents | ✓ | random sampling |
| **Domain-aware scoring** — Harness Jurors are calibrated against your real system prompt, knowledge corpus, and tool schemas | ✓ | generic |
| **Multi-turn adversarial conversations** with callbacks and follow-up probes | ✓ | rare |
| **3-Harness-Juror Delphi consensus** — independent re-vote on disagreement | ✓ | single judge |
| **Guaranteed coverage** — every plan reserves ≥30% of slots for prompt injection + hallucination probes, plus ≥2 mandatory factuality traps drawn from documented production incidents (Mata v. Avianca, Walters v. OpenAI, Moffatt v. Air Canada) | ✓ | hope and pray |
| **64 bundled traps across 11 families** (GDPR / CCPA / HIPAA / PCI / SOX / prompt injection / social engineering / tool misuse / …) | ✓ | usually no |
| **Skills-as-files** (Claude-Skills aligned) — your team can read and fork | ✓ | hardcoded |
| **Bring-your-own LLM** (Anthropic / OpenAI / Gemini / Bedrock / Ollama / vLLM) | ✓ | provider-locked |
| **Local-first** — your context never leaves the machine | ✓ | upload required |
| **pytest integration** with assertion-style thresholds | ✓ | usually web UI only |

## Where it fits in your stack

| Use it for | Use something else for |
|---|---|
| **Pre-launch readiness gates** — "should this agent ship?" | Single-prompt regression checks → Promptfoo |
| **Production-readiness audits** — multi-turn adversarial coverage | Inline guardrails at runtime → Guardrails / NeMo Guardrails |
| **Regression detection across releases** — same agent + new model / prompt | Token-level LLM observability → Langfuse / Helicone |
| **Compliance evidence** — GDPR / HIPAA / PCI / SOX coverage | A/B prompt experimentation → Promptfoo |

You can use them together: Promptfoo for prompt-engineering iteration, this harness for production-readiness gates, runtime guardrails for live mitigation.

## Next

- [How it works →](how-it-works.md) — the 5-agent pipeline in detail
- [The 5 metrics →](metrics.md) — what gets scored, and how certification works
