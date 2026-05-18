# Your agent + AgentContext

[← back to docs](README.md)

The agent under test is just a Python callable. Three shapes, in increasing depth.

## 1. Plain function (stateless)

Simplest. The harness calls your function once per turn with the user message and expects a string back.

```python
from proofagent_harness import Harness

def my_agent(message: str) -> str:
    return your_llm_call(message)

Harness().evaluate(my_agent, role="customer support", goal="handle refunds safely")
```

Good for stateless inference, RAG agents that maintain context internally, or quick smoke tests.

## 2. Closure (stateful, no class needed)

Pass conversation history across turns without writing a class:

```python
def make_agent():
    history = []
    def agent(message: str) -> str:
        history.append({"role": "user", "content": message})
        text = your_llm_call(messages=history)
        history.append({"role": "assistant", "content": text})
        return text
    return agent

Harness().evaluate(make_agent(), role="...", goal="...")
```

The conductor's callbacks across turns work much better when the agent has multi-turn memory.

## 3. Return `AgentResponse` for deep scoring

Expose **what the agent did under the hood** — tool calls, retrievals, memory snapshots — so the Harness Jurors can score tool picking, retrieval grounding, and memory stability properly.

```python
from proofagent_harness import AgentResponse, Harness

def agent(message: str) -> AgentResponse:
    text, tools, retrievals = run_my_agent(message)
    return AgentResponse(
        text=text,
        tools_called=tools,         # [{"name": "lookup_order", "args": {...}, "result": ...}]
        retrievals=retrievals,      # [{"source": "policy.md", "chunk": "...", "score": 0.91}]
        memory_snapshot={"verified": True, "case_id": "REF-123"},
        reasoning="optional chain-of-thought / scratchpad text",
    )

Harness().evaluate(agent, role="...", goal="...")
```

See [`examples/03_stateful_agent_with_response.py`](../examples/03_stateful_agent_with_response.py) for a runnable version.

---

## AgentContext

`AgentContext` gives the harness the same artifacts you'd hand a new engineer onboarding to the agent — system prompt, knowledge corpus, tool schemas, prior memory. **Without it, scoring caps fire** (see [Metrics → Limited-context cap](metrics.md#limited-context-cap)).

### Inline

```python
from proofagent_harness import AgentContext, Harness

Harness().evaluate(
    agent,
    role="customer support",
    goal="handle refunds safely",
    context=AgentContext(
        system_prompt=open("system.md").read(),
        knowledge="./knowledge/",                   # dir, file path, list of paths, dict, or raw text
        tools=open("tools.json").read(),            # JSON tool schemas (Anthropic or OpenAI format)
        memory=[{"role": "user", "content": "earlier session..."}],
        few_shots=[("Q: …", "A: …"), …],            # optional calibration examples for the jurors
        metadata={"agent_version": "v2.4"},         # free-form tags (useful for regression tracking)
    ),
)
```

### Auto-discover from a directory

```
my_agent/
├── system_prompt.md
├── tools.json
├── memory.jsonl              # one {"role": "...", "content": "..."} per line
├── few_shots.jsonl           # one {"q": "...", "a": "..."} per line
├── metadata.json
└── knowledge/                # or knowledge.md
    ├── refund_policy.md
    └── pci_handling.md
```

```python
ctx = AgentContext.from_dir("./my_agent/")
Harness().evaluate(agent, role="...", goal="...", context=ctx)
```

See [`examples/04_with_full_context.py`](../examples/04_with_full_context.py).

### Or load from YAML / JSON

```python
ctx = AgentContext.from_file("agent_context.yaml")
```

---

## What gets scored without context

If you skip `AgentContext`, the harness still runs but the scoring caps automatically:

| Missing | Capped metric |
|---|---|
| No `system_prompt` | `instruction_following` capped at 5/10 |
| No `knowledge` | `hallucination_resistance` capped at 8/10 |
| No `tools` | `tool_picking` scoring downgraded |

This is intentional — without these artifacts, the Harness Jurors can't fairly tell whether the agent is following the spec or hallucinating. The scorecard surfaces a yellow warning so the cap is explicit, not silent.

## Next

- [The 5 metrics →](metrics.md) — what each one is scoring and how findings get typed
- [CI integration →](ci-integration.md) — wrap this in a pytest assertion
- [Configuration →](configuration.md) — all the `Harness(...)` knobs
