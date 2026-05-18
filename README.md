<div align="center">

# proofagent-harness

**Open-source, domain-aware test harness for AI agents.**

[![PyPI](https://img.shields.io/pypi/v/proofagent-harness.svg)](https://pypi.org/project/proofagent-harness/)
[![Python](https://img.shields.io/pypi/pyversions/proofagent-harness.svg)](https://pypi.org/project/proofagent-harness/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/ProofAgent-ai/proofagent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/ProofAgent-ai/proofagent-harness/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-154%20passing-brightgreen.svg)](tests/)

</div>

Trusted by teams shipping production AI agents — `proofagent-harness` is the open-source, domain-aware test harness for evaluating, stress-testing, and red-teaming agents before they reach users. Multi-turn adversarial scenarios, three Harness Jurors scoring across five production-critical metrics, 64 bundled traps across 11 families, bring-your-own LLM. Run locally, in CI, or scale through [ProofAgent Platform](https://proofagent.ai/platform).

```bash
pip install -U proofagent-harness
```

```python
from proofagent_harness import Harness

def my_agent(message: str) -> str:
    return your_llm_call(message)

report = Harness().evaluate(my_agent, role="customer support", goal="handle refunds safely")
print(report)
```

```
proofagent-harness — Scorecard
│ Task Success            │  9.0 / 10 │ pass     │
│ Hallucination Resistance│  8.0 / 10 │ pass     │
│ Safety                  │ 10.0 / 10 │ pass     │
│ Instruction Following   │  9.0 / 10 │ pass     │
│ Manipulation Resistance │  8.0 / 10 │ pass     │
Final score: 8.80 / 10    Certification: SILVER    Tokens: 51,518
```

## Documentation

The full reference lives in [`docs/`](docs/README.md). Quick links:

<table>
<tr><th>Get started</th><th>Capabilities</th><th>Red Teaming</th><th>Reference</th></tr>
<tr><td valign="top">

📦 [Install](docs/install.md)
⚡ [Quickstart](docs/quickstart.md)
💡 [Why](docs/why.md)

</td><td valign="top">

🏗️ [How it works](docs/how-it-works.md)
📐 [The 5 metrics](docs/metrics.md)
🧩 [Your agent + Context](docs/your-agent.md)
🧪 [CI integration](docs/ci-integration.md)
💻 [CLI + Recipes](docs/cli.md)
⚙️ [Configuration](docs/configuration.md)
🎲 [Reproducibility](docs/reproducibility.md)

</td><td valign="top">

🎯 [Traps & skills](docs/traps.md)
📜 [Trap manifest v1.0](docs/TRAP_MANIFEST.md)
🔧 [Bring your own traps](docs/red-teaming.md)

</td><td valign="top">

🆚 [Open source vs Hosted](docs/platform-vs-hosted.md)
❓ [FAQ](docs/faq.md)
📂 [Examples](examples/)
📓 [Notebooks](notebooks/)

</td></tr>
</table>

## Why proofagent-harness?

Most AI eval libraries score the **last response** with **one judge** against a **fixed test set**. Production agents fail differently — in the third turn under social-engineering pressure, via domain-specific failure modes (HIPAA leaks, PCI handling, malware-gen), through callbacks that weaponize an earlier concession. This harness is built for that.

- **Domain-aware planning + scoring** — picks HIPAA traps for healthcare, PCI for retail, malware-gen probes for code agents. Harness Jurors are calibrated against your real system prompt, knowledge corpus, and tool schemas. → [Why](docs/why.md)
- **Multi-turn campaigns with callbacks** — the conductor weaves follow-ups across turns, not isolated prompts. → [How it works](docs/how-it-works.md)
- **3-Harness-Juror Delphi consensus** — independent re-vote on disagreement. No single LLM call decides the verdict. → [Configuration](docs/configuration.md)
- **64 bundled traps across 11 families** including GDPR / CCPA / HIPAA / PCI / SOX. Add your own as `.md` files. → [Traps](docs/traps.md)
- **Bring-your-own LLM** (Anthropic / OpenAI / Gemini / Bedrock / Ollama / vLLM) — local-first, your context never leaves the machine. → [Install](docs/install.md)
- **pytest integration** with assertion-style thresholds. → [CI integration](docs/ci-integration.md)

## Scale through ProofAgent Platform

`proofagent-harness` is the local OSS test harness. The [hosted Platform](https://proofagent.ai/platform) adds dashboards, production log replay, artifact grading, multi-agent trace scoring, expert human review, regression tracking, proprietary domain MCP, and team RBAC + audit logs. Both speak the same vocabulary (same metrics, same trap families, same readiness tiers).

→ [Open source vs Hosted](docs/platform-vs-hosted.md)

## Contributing

PRs welcome. Highest-leverage contributions: a new trap (one `.md` file following [`docs/TRAP_MANIFEST.md`](docs/TRAP_MANIFEST.md)) or a new persona (different Harness Juror voices catch different failure modes). Code: `pip install -e ".[dev]"` then `pytest`. Full guide: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License & attribution

Apache 2.0 — see [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).

- **Copyright** © 2025-2026 **ProofAI LLC**
- **Original author** **Dr. Fouad Bousetouane**

"ProofAgent" and "ProofAgent Harness" are trademarks of ProofAI LLC. The Apache 2.0 license grants rights to use, modify, and distribute the software, but does not grant rights to use the ProofAgent name, logo, or branding for competing hosted services.

---

<div align="center">
<sub>Built by the team behind <a href="https://proofagent.ai">ProofAgent</a>. Star us on GitHub if this saved you an incident.</sub>
</div>
