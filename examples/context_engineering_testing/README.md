# `context_engineering_testing/` — the agent context the example grades

This folder **is** the input to the context-engineering assessment. It mirrors
how a production agent is actually shipped — its **system prompt**, its **tool
schemas**, and its **knowledge base** — as plain files. The example
[`../12_context_engineering.py`](../12_context_engineering.py) loads it with
`AgentContext.from_dir()` and, with `assess_context=True`, the harness grades the
**quality of this context** (role clarity, guardrails, tool-schema quality,
grounding, injection hardening, token efficiency) as a **separate** sub-score
that never affects the metric scores or the gate.

`AgentContext.from_dir()` auto-discovers these files by name:

| File | Maps to | Required? | What it is |
|---|---|---|---|
| `system_prompt.md` | `AgentContext.system_prompt` | **one of** these two | The agent's production system prompt. |
| `tools.json` | `AgentContext.tools` | **one of** these two | The agent's tool schemas (JSON; Anthropic/OpenAI shape). |
| `knowledge.md` *(or `knowledge/`)* | `AgentContext.knowledge` | optional | Grounding corpus for hallucination scoring. |
| `memory.jsonl` | `AgentContext.memory` | optional | Prior `{"role","content"}` messages (multi-session continuity). |
| `few_shots.jsonl` | `AgentContext.few_shots` | optional | `{"q","a"}` calibration examples. |
| `metadata.json` | `AgentContext.metadata` | optional | Free-form tags (e.g. agent version). |

The assessment runs as long as there's a **`system_prompt.md` or `tools.json`** —
with neither, there's nothing to grade and it no-ops to `{}`.

## Try it with YOUR agent

Copy this folder, drop in your real `system_prompt.md` + `tools.json` (+ optional
`knowledge.md`), and point the example at it:

```bash
python examples/12_context_engineering.py --context-dir ./my_agent_context/
```

The files here are deliberately **improvable** (a vague role, tool schemas with
no typed args or when-to-call guidance, no untrusted-data separation) so the
panel surfaces concrete findings — each with a fix and a token-impact verdict.
