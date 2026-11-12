#!/usr/bin/env bash
# LOCAL e2e test: hardest multi-turn run on gpt-5-nano, PER export, upload to local governance.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
export AGENT_LLM=gpt-5-nano AGENT_REASONING_EFFORT=low LITELLM_LOG=ERROR
mkdir -p results/run20
../../.venv/bin/proof run agent.py \
  --context-dir context \
  --domain-knowledge-dir domain_knowledge \
  --assess-context \
  --assess-compliance \
  --assess-governance \
  --llm gpt-5-nano --reasoning-effort low \
  --turns 20 \
  --fresh \
  --seed -1 \
  --consensus debate \
  --json results/run20/e2e.json \
  --markdown results/run20/e2e.md \
  --per results/run20/e2e.per.json \
  --agent credit-allocation-agent \
  --agent-version e2e-nano-20t-v4 \
  --profile financial_services_default \
  --upload \
  --upload-transcript \
  --fail-on block \
  --source manual
