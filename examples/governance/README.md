# Governance example — a real airline refund agent, end to end

A complete, realistic governance evaluation you can run and ship to the
ProofAgent Governance dashboard. Unlike a toy agent, this one has the three
things a real evaluation needs:

| Ingredient | File | Why it matters |
|---|---|---|
| A real **agent under test** | `airline_refund_agent.py` | LLM-backed, tool-using (function calling), with a production system prompt + conversation memory |
| **Context** (system prompt + tools) | passed via `AgentContext` | the instruction-following juror sees the real prompt; the tool-use juror sees the tool schemas |
| **Prior knowledge** (ground truth) | `knowledge/refund_policy.md` | the hallucination juror checks the agent's claims against the actual refund policy |

```
examples/governance/
├── airline_refund_agent.py    the agent under test (exposes `agent`)
├── knowledge/refund_policy.md  the AcmeAir refund policy corpus (grounding)
├── run_governance_eval.py      recommended runner — full context + upload + gate
└── README.md
```

## Run it (recommended — full context)

```bash
export OPENAI_API_KEY=sk-...                       # Harness LLM + agent (gpt-4.1-mini)

# optional: upload to the dashboard (ProofAgent Cloud) and gate on the decision
export PROOFAGENT_API_KEY=pa_live_xxx              # dashboard → Settings → API keys

python examples/governance/run_governance_eval.py
```

> The CLI is Cloud-locked. To test against a local backend, call the
> `upload_run(api_url="http://localhost:8000")` Python API directly.

What happens: the harness reads the role + business case + the agent's system
prompt + tools to **infer the domain (LLM-based)** and select **relevant** traps
(refund abuse, identity-verification bypass, tool misuse, PII probing,
manipulation) — not generic healthcare/devops traps. The agent answers each
adversarial turn using its tools; jurors score it and ground its claims against
`refund_policy.md`; the run uploads and returns `PASS` / `REVIEW` / `BLOCK`. The
process exits `0/1/2` so it works as a CI gate.

Knobs (env): `HARNESS_LLM` (default `gpt-4.1-mini`), `AGENT_MODEL`,
`TURNS` (default 8), `AGENT_VERSION`, `FAIL_ON` (default `block`).

## Or via the CLI

```bash
proof run examples/governance/airline_refund_agent.py \
  --role "Airline customer-support refund agent" \
  --business-case "Handles flight refunds, cancellations and booking changes; verify identity, never leak PII, escalate out-of-policy exceptions" \
  --goal "Resolve refund and booking requests within policy" \
  --knowledge examples/governance/knowledge/refund_policy.md \
  --turns 8 --agent "Airline Refund Agent" --agent-version v1.0.0 \
  --profile airline_customer_support --llm gpt-4.1-mini \
  --upload --fail-on block
```
(The CLI passes `--knowledge` + role/business-case; the programmatic runner above
additionally passes the system prompt + tool schemas as `AgentContext`, which the
jurors use — so the runner gives the highest-fidelity score.)

## Use it as a template
Swap the body of `agent()` to call **your** agent/service, update `SYSTEM`,
`TOOLS`, and `knowledge/refund_policy.md` to your domain, and adjust the
`role` / `business_case` / `goal` in `run_governance_eval.py`. Everything else —
domain-relevant trap selection, scoring, grounding, the gate, and the upload —
works unchanged.

> No LLM key handy? See `../11_governance_gate.py`, which uploads a saved
> evaluation report to the dashboard without making any model calls.
