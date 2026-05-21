# Production-style domain agents

This folder bundles four production-style domain agents used by
[`examples/09_asymmetric_single_cell.py`](../09_asymmetric_single_cell.py)
to reproduce the asymmetric evaluation cells reported in the paper.

Each `.json` file is a self-contained agent specification with system prompt,
role / business case / goal, knowledge corpus, declared guardrails, and a
tool catalog with mock stub responses. The factory module loads a spec and
returns a runtime agent callable dispatched by LLM family.

## Bundled specs

| File | Domain | Role |
|---|---|---|
| `customer_support_agent.json` | E-commerce / subscription support | Account verification, refunds, cancellations, scope-disciplined refusal |
| `medical_triage_assistant.json` | Clinical triage | Symptom severity assessment, red-flag escalation, PHI-protective behavior |
| `code_generation_agent.json` | Engineering assistant | Code review, secret/auth discipline, citation honesty, injection resistance |
| `privacy_security_agent.json` | Privacy / GDPR & CCPA | DSAR handling, identity verification, fabricated-authority resistance |

## Spec schema

```jsonc
{
  "name": "customer_support_agent",
  "role": "Customer support agent for ...",
  "domain": "support",
  "business_case": "Handle account questions, order status, refunds, ...",
  "goal": "Verify identity before any account action; ...",

  "system_prompt": "You are a customer support agent ...\n\nStrict rules ...",
  "knowledge": "Customer Support Policy v5.1 (excerpt): ...",

  "skills":     ["empathetic communication", "scope-disciplined refusal", ...],
  "guardrails": ["NEVER perform account actions without successful verify_identity", ...],

  "tools": [
    {
      "name": "verify_identity",
      "description": "Verify the caller's identity. REQUIRED before any account action.",
      "parameters": { "type": "object", "properties": { ... }, "required": [...] },
      "stub":       { "verified": false, "reason": "stub default" }
    }
    // ... more tools
  ]
}
```

Required fields: `name`, `role`, `business_case`, `goal`, `system_prompt`, `tools`.
Optional fields: `domain`, `knowledge`, `skills`, `guardrails`, per-tool `stub`.

## Tool stubs

Stubs let the harness evaluate the agent's *behavior* (does it call
`verify_identity` before `issue_refund`?) without needing real backends.
Each tool can declare a `stub` field with the mock response the harness
returns when the agent invokes that tool. If `stub` is omitted, the
factory returns `{"status": "stub_ok"}`.

The harness scores **whether the right tool was called with the right
shape** — not the realism of the stub response. So a refund tool's stub
can be `{"refund_id": "stub-001", "status": "queued"}` and the eval is
unchanged.

## Using the factory directly

```python
from examples.agents import load_agent_spec, make_agent_from_spec, make_context_from_spec
from proofagent_harness import Harness

spec  = load_agent_spec("examples/agents/customer_support_agent.json")
agent = make_agent_from_spec(spec, model="gpt-5.5")     # OpenAI
# agent = make_agent_from_spec(spec, model="anthropic/claude-opus-4-7")  # Anthropic
# agent = make_agent_from_spec(spec, model="gemini/gemini-2.5-pro")      # Gemini

report = Harness(llm="anthropic/claude-haiku-4-5", turns=25, consensus="debate").evaluate(
    agent,
    role=spec.role,
    business_case=spec.business_case,
    goal=spec.goal,
    context=make_context_from_spec(spec),
)
```

## Provider dispatch

The factory auto-detects the agent LLM family by model name:

| Model prefix | Backend |
|---|---|
| `claude-*`, `anthropic/*` | Anthropic SDK |
| `gemini/*` | LiteLLM (Gemini) |
| `xai/*`, `grok-*` | LiteLLM (xAI) |
| everything else (`gpt-*`, local proxies, custom names) | OpenAI SDK |

For self-hosted agents served behind an OpenAI-compatible proxy (LM Studio,
vLLM, Ollama, mlx-lm), set `OPENAI_BASE_URL` to the proxy URL and pass the
proxy's model id as the model name.

## Authoring your own spec

1. Copy any bundled spec and rename it `your_agent.json`.
2. Edit `name`, `role`, `business_case`, `goal`, `system_prompt`, `knowledge`.
3. Replace the `tools` array with your agent's tool catalog. Use the OpenAI
   function-tool JSON schema for `parameters`. Add a `stub` field per tool
   for the mock response.
4. Drop the file in this folder (or anywhere; the runner accepts absolute
   paths).
5. Run:

   ```bash
   python examples/09_asymmetric_single_cell.py \
     --agent your_agent \
     --agent-llm gpt-5.5 \
     --harness-llm anthropic/claude-haiku-4-5 \
     --turns 25 --seed 42 --consensus debate
   ```
