# Example: a full agent project (credit-allocation agent)

A complete, copy-me example that shows **how to feed your own files** into the harness for
both evaluation modes. The agent is a consumer credit-card application reviewer (a realistic,
regulated domain), but the **structure is the point** — swap the contents for your own agent.

It demonstrates the two separate inputs the harness takes:

| Input | Flag | What goes in it |
|---|---|---|
| **The agent** | `--context-dir` | system prompt, tool schemas, memory, and a manifest (role / goal / business-case) |
| **The domain** | `--domain-knowledge-dir` | grounding docs the agent must follow (policy, regulations, product rules) — any `.md / .txt / .json / .yaml` |

Keeping them separate means you can reuse one domain corpus across many agents, and swap an
agent without touching the domain (and vice-versa).

---

## Layout

```
credit_agent/
├── agent.py                     # the agent UNDER TEST (a real LiteLLM callable)
├── run_multi_turn.sh            # proof run …  (adversarial conversation)
├── run_artifact.sh              # proof artifact …  (grade a finished deliverable)
│
├── context/                     # --context-dir : THE AGENT
│   ├── agent.yaml               #   role / goal / business-case (the run intent)
│   ├── system_prompt.md         #   the agent's own instructions
│   ├── tools.json               #   the agent's tool schemas (OpenAI/Anthropic format)
│   └── memory.jsonl             #   prior-session messages (optional)
│
├── domain_knowledge/            # --domain-knowledge-dir : THE DOMAIN (grounding corpus)
│   ├── credit_policy.md
│   ├── regulatory_ecoa_fcra.md
│   └── product_catalog.md
│
└── artifact/                    # inputs for artifact mode
    ├── credit_decision_report.md    # the deliverable to grade
    └── corpus/                      # --domain-knowledge-dir : ground truth to grade against
        ├── applicant_file.md
        ├── credit_policy.md
        └── regulatory_requirements.md
```

`AgentContext.from_dir("context/")` auto-discovers the files in `context/`. The manifest
(`agent.yaml`) supplies role / goal / business-case, so you don't repeat them on the CLI.

---

## Run it

From the repo root, with a Harness LLM key exported (e.g. `OPENAI_API_KEY`, and
`ANTHROPIC_API_KEY` for the fallback):

```bash
# 1) MULTI-TURN — stress-test the live agent under adversarial pressure
bash examples/credit_agent/run_multi_turn.sh

# 2) ARTIFACT — grade a finished credit decision report against ground truth
bash examples/credit_agent/run_artifact.sh
```

Or call the CLI directly (this is what the scripts run):

```bash
proof run examples/credit_agent/agent.py \
  --context-dir examples/credit_agent/context \
  --domain-knowledge-dir examples/credit_agent/domain_knowledge \
  --assess-context

proof artifact examples/credit_agent/artifact/credit_decision_report.md \
  --type report \
  --domain-knowledge-dir examples/credit_agent/artifact/corpus \
  --assess-context
```

Add `--upload` to push the run to the governance dashboard and gate on the decision
(needs `PROOFAGENT_API_KEY` — get one at https://app.proofagent.ai → Settings → API Keys).
The name you pass as `--agent` is what the run appears under on the dashboard.

---

## A deliberately weak baseline

The bundled `context/system_prompt.md` is a **thin "v1"** — it intentionally leaves out the hard
guardrails a production credit agent needs (strict tier ceilings, PII rules, fair-lending and
adverse-action requirements, tool-honesty, "no user policy overrides"). Scored against the strong
`goal` in `agent.yaml`, with `gpt-4.1` as the juror and `debate` consensus (both harsher), it lands
in **NEEDS_ENHANCEMENT / NOT_READY** on purpose. That's the teaching point: read the findings,
harden the prompt, re-run, and watch the score climb — this is a *before*, not a finished agent.

## What it exercises

- **Multi-turn:** the harness plays an adversary that probes exactly what the agent's rules
  forbid — PII leakage, pressure to over-approve, false-premise policy claims, prompt injection —
  then scores the transcript across the six metrics with zero-tolerance caps. Grounding it in
  `domain_knowledge/` lets the hallucination juror check the agent against the real policy.
- **Artifact:** the report in `artifact/credit_decision_report.md` has planted, realistic
  issues (an over-policy limit, an out-of-range APR, an unsupported "zero late payments" claim,
  a marital-status rationale, a vague adverse-action reason), so the evaluation returns concrete,
  evidence-linked findings graded against `artifact/corpus/`.

---

## Make it your own

1. Replace `context/system_prompt.md`, `context/tools.json`, and `context/agent.yaml` with your
   agent's real prompt, tools, and role/goal/business-case.
2. Drop your grounding docs into `domain_knowledge/` (or point `--domain-knowledge-dir` anywhere).
3. Wire your real agent in `agent.py` (any callable that takes a message string and returns a
   string or an `AgentResponse`).
4. Run the two commands above.

Python API equivalent:

```python
from proofagent_harness import Harness, AgentContext

report = Harness(llm="gpt-4.1-mini").evaluate(
    my_agent,
    context=AgentContext.from_dir("examples/credit_agent/context"),  # the agent
    knowledge="examples/credit_agent/domain_knowledge",              # the domain
    assess_context=True,
)
print(report.final_score, report.certification)
```
