#!/usr/bin/env bash
# LOCAL e2e test: hardest multi-turn run on gpt-5-nano, PER export, upload to local governance.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
export LM_STUDIO_API_BASE=http://localhost:1234/v1 PROOFAGENT_JURY_CONCURRENCY=4 AGENT_LLM=gpt-5-nano AGENT_REASONING_EFFORT=low LITELLM_LOG=ERROR
mkdir -p results/gemma
../../.venv/bin/proof run agent.py \
  --context-dir context \
  --domain-knowledge-dir domain_knowledge \
  --assess-context \
  --assess-compliance \
  --assess-governance \
  --llm lm_studio/google/gemma-4-26b-a4b-qat \
  --turns 25 \
  --fresh \
  --seed -1 \
  --consensus debate \
  --json results/gemma/e2e.json \
  --markdown results/gemma/e2e.md \
  --per results/gemma/e2e.per.json \
  --agent credit-allocation-agent \
  --agent-version e2e-gemma-25t-fixed \
  --profile financial_services_default \
  --upload \
  --upload-transcript \
  --fail-on block \
  --source manual
