#!/usr/bin/env bash
#
# Multi-turn adversarial evaluation of the example credit-allocation agent.
#
#   bash run_multi_turn.sh            # local (offline, writes a report)
#   bash run_multi_turn.sh --upload   # + push to the governance dashboard and gate
#
# Two separate inputs:
#   --context-dir          the AGENT   (system_prompt.md, tools.json, memory.jsonl, agent.yaml)
#   --domain-knowledge-dir the DOMAIN  (policy, regulations, product tiers — grounding docs)
#
# role / goal / business-case come from context/agent.yaml, so they don't need repeating here.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p results

proof run agent.py \
  --context-dir context \
  --domain-knowledge-dir domain_knowledge \
  --assess-context \
  --llm gpt-4.1 \
  --fallback-llm anthropic/claude-haiku-4-5 \
  --consensus debate \
  --json results/credit_agent.json \
  --markdown results/credit_agent.md \
  --agent credit-allocation-agent \
  --agent-version "$(git rev-parse --short HEAD 2>/dev/null || echo example)" \
  --profile financial_services_default \
  --fail-on block \
  --source manual \
  "$@"
