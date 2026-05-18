# FAQ

[← back to docs](README.md)

## How is this different from Promptfoo or DeepEval?

Promptfoo and DeepEval are excellent for **single-shot** evaluation — give them an input, they score the output. `proofagent-harness` is built for **multi-turn adversarial** evaluation: the conductor escalates pressure across turns, blends attack vectors, and exploits the agent's prior responses. The 3-Harness-Juror Delphi consensus (re-voting on disagreement) is also unique to this library.

Use them together: Promptfoo for prompt-engineering iteration, this harness for production-readiness gates.

## Does this work with my LangChain / LangGraph / CrewAI agent?

Yes. Wrap your existing agent in a 5-line adapter:

```python
from proofagent_harness import Harness, AgentResponse
from my_app import my_existing_agent

def agent(message: str) -> AgentResponse:
    result = my_existing_agent.invoke({"input": message})
    return AgentResponse(
        text=result["output"],
        tools_called=result.get("intermediate_steps", []),
    )

Harness().evaluate(agent, role="...", goal="...")
```

Same pattern works for **OpenAI Agents SDK**, **AutoGen**, **Semantic Kernel**, **LlamaIndex**, **MCP**-style servers, and any custom agent loop you've written.

## How many LLM calls does one run make?

A typical 8-turn evaluation with `delphi` consensus runs **~38 LLM calls in ~30 seconds**:

| Stage | Calls |
|---|---:|
| Planner (incl. domain inference + weaving) | 2-3 |
| Conductor (8 turns + your agent) | 16 |
| Harness Jury Round 1 (3 personas × 5 metrics) | 15 |
| Harness Jury Round 2 re-votes (~30% of metrics) | ~5 |
| Reporter | 1 |

Mix models to save cost — use a smaller Harness LLM while your agent stays on its production model:

```python
Harness(llm="claude-haiku-4-5-20251001")
```

Token usage shows up on `report.tokens_used` and is rendered next to the certification on the auto-printed scorecard.

## Can I run it without an API key for testing?

Yes — tests use a `FakeLLM` fixture (see `tests/conftest.py`). Adopt the same pattern in your CI for hermetic dry-runs that exercise the pipeline without spending tokens.

## How do I add traps for my own domain?

Drop `.md` files in a directory following the [trap manifest spec](TRAP_MANIFEST.md), then:

```python
Harness(extra_traps=["./my_traps/"]).evaluate(...)
```

Or use [`examples/08_custom_trap.py`](../examples/08_custom_trap.py) which ships with `--trap`, `--list-only`, full LLM choice, and optional proxy juror flags. Full walkthrough: [Bring your own traps](red-teaming.md).

## What about safety — can the conductor produce harmful content?

The conductor is designed to **elicit failure modes from the agent under test**, not to generate harmful content directly. Trap definitions describe the attack pattern, not harmful payloads. The conductor's prompt explicitly forbids generating CSAM, malware, weapons synthesis, or any content that is itself harmful — the test is whether **the agent** produces it, not whether the conductor does.

## Does Anthropic's `seed` parameter work?

Not yet — Anthropic doesn't currently honor `seed`. Use OpenAI or Gemini for tightest reproducibility, or run the eval N times and report `median + IQR`. See [Reproducibility](reproducibility.md).

## How long does an eval take?

| Profile | Turns | Consensus | Wall clock | LLM calls |
|---|---:|---|---:|---:|
| Smoke | 4 | `independent` | ~30s | ~15 |
| Production (default) | 8 | `delphi` | ~3-5 min | ~38 |
| Stability check | 8 × 5 runs | `delphi` | ~15-25 min | ~190 |
| High-stakes | 15 | `debate` | ~10-15 min | ~120 |

Most of the wall-clock time is the conductor + jury making sequential calls. For faster iteration, use a smaller Harness LLM (`claude-haiku-4-5` or `gpt-4.1-mini`).

## Can I run the Harness LLM locally for free?

Yes — point at any OpenAI-compatible local server (Ollama, vLLM, LM Studio, mlx):

```bash
export OPENAI_BASE_URL=http://localhost:1234/v1
export OPENAI_API_KEY=not-required-for-local
proof run my_agent.py --llm openai/gemma-4-e4b-it-mlx --turns 8 --ctx 6000
```

See [`examples/07_proxy_llm_agent.py`](../examples/07_proxy_llm_agent.py) for a worked example. Smaller local models work great as the Harness LLM while your agent stays on its production model. For air-gapped enterprise deployments, this combo gives you fully-local evaluation.

## Where do I find the bundled trap library on disk?

```bash
python -c "from proofagent_harness import __file__; import os; print(os.path.join(os.path.dirname(__file__), 'data/traps'))"
```

Or browse it on GitHub: [`src/proofagent_harness/data/traps/`](../src/proofagent_harness/data/traps/).

## How do I report a bug or request a feature?

Open an issue on [GitHub](https://github.com/ProofAgent-ai/proofagent-harness/issues). For security issues, see [`SECURITY.md`](../SECURITY.md).
