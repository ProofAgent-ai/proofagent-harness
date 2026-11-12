#!/usr/bin/env bash
# Evaluate the real credit agent with a LOCAL jury LLM (LM Studio) and upload the findings.
#   agent under test : agent.py on $AGENT_LLM (default gpt-5-nano, real OpenAI call)
#   harness LLM/jury : Gemma 4 26B served by LM Studio on this machine
#   upload target    : $PROOFAGENT_API_BASE_URL from ../../.env (the governance API)
# Usage: ./run_local_upload.sh [turns]   (default 8)
set -euo pipefail
cd "$(dirname "$0")"
_KEY="${UPLOAD_KEY:-}"; _URL="${UPLOAD_URL:-}"
set -a; . ../../.env; set +a
[ -n "$_KEY" ] && export PROOFAGENT_API_KEY="$_KEY"      # override the upload key from .env
[ -n "$_URL" ] && export PROOFAGENT_API_BASE_URL="$_URL" # override the upload target from .env
SEED="${SEED:-42}"
export LM_STUDIO_API_BASE=http://localhost:1234/v1 LITELLM_LOG=ERROR
export AGENT_LLM="${AGENT_LLM:-gpt-5-nano}" AGENT_REASONING_EFFORT=low PROOFAGENT_JURY_CONCURRENCY=2
TURNS="${1:-8}"; OUT=results/local_upload; mkdir -p "$OUT"
echo "Agent: $AGENT_LLM | Jury: lm_studio/google/gemma-4-26b-a4b-qat | Turns: $TURNS | Upload: $PROOFAGENT_API_BASE_URL"
../../.venv/bin/proof run agent.py \
  --context-dir context \
  --domain-knowledge-dir domain_knowledge \
  --assess-context \
  --assess-compliance \
  --llm lm_studio/google/gemma-4-26b-a4b-qat \
  --turns "$TURNS" \
  --seed "$SEED" \
  --json "$OUT/run_s$SEED.json" \
  --markdown "$OUT/run_s$SEED.md" \
  --per "$OUT/run_s$SEED.per.json" \
  --consensus "${CONSENSUS:-independent}" \
  --agent "${AGENT_NAME:-credit-allocation-agent}" \
  --agent-version "${AGENT_VERSION:-local-gemma}" \
  --profile financial_services_default \
  --upload \
  --fail-on block \
  --source manual \
  --environment development
