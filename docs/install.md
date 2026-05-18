# Install

[← back to docs](README.md)

```bash
pip install proofagent-harness
```

Requires **Python 3.10+**.

## Configure your model

The harness uses [LiteLLM](https://github.com/BerriAI/litellm) under the hood, so anything LiteLLM supports works as the Harness LLM (planner, conductor, Harness Jurors, reporter):

```bash
# Anthropic (default)
export ANTHROPIC_API_KEY=sk-ant-...

# OR OpenAI
export OPENAI_API_KEY=sk-...
export PROOFAGENT_LLM=gpt-4.1-mini

# OR Gemini
export GEMINI_API_KEY=...
export PROOFAGENT_LLM=gemini/gemini-1.5-pro

# OR Bedrock / Vertex / Mistral / Cohere / Ollama / vLLM — see LiteLLM docs
```

Your agent under test is your own choice entirely — it doesn't need to be on the same provider as the harness.

## Choosing a model — practical guidance

| Goal | Recommended |
|---|---|
| **Production-grade evals** | Claude Sonnet 4.6 or GPT-4.1 (for both harness + agent) |
| **Tightest reproducibility** | GPT-4.1 / Gemini 1.5 Pro with `seed=42` *(Anthropic doesn't honor `seed` yet)* |
| **Largest context (huge corpora)** | Gemini 1.5 Pro (2M) or GPT-4.1 (1M) |
| **Lightweight CI** | Haiku / GPT-4.1-mini as the Harness LLM, your agent stays on whatever it normally runs |
| **Air-gapped / on-prem** | Ollama or a vLLM/TGI-served model |

## Verify the install

```bash
python -c "from proofagent_harness import Harness; print('OK')"
proof --help
```

You should see the Typer CLI help. Next: [Quickstart →](quickstart.md)
