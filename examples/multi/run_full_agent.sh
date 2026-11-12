#!/usr/bin/env bash
# Full test of any spec agent: AGENT=<slug> (e.g. banking-dispute-agent; SPEC derived: banking_dispute_agent.json).
# Harness LLM: HARNESS_LLM (default lm_studio/prism-ml/bonsai-27b). Agent LLM: AGENT_LLM (default gpt-5-nano).
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
export PROOFAGENT_JURY_CONCURRENCY="${PROOFAGENT_JURY_CONCURRENCY:-2}"  # 4 parallel 64k caches beside the agent model swapped the 64 GB machine
AGENT="${AGENT:?set AGENT, e.g. banking-dispute-agent}"
export SPEC="${SPEC:-${AGENT//-/_}.json}" AGENT_LLM="${AGENT_LLM:-gpt-5-nano}" LM_STUDIO_API_BASE="${LM_STUDIO_API_BASE:-http://localhost:1234/v1}" LITELLM_LOG=ERROR
export PROOFAGENT_AGENT_MODEL="${PROOFAGENT_AGENT_MODEL:-$AGENT_LLM}"  # names the agent's model in the PER
export PROOFAGENT_API_BASE_URL="${UPLOAD_URL:-https://app.proofagent.ai}" PROOFAGENT_API_KEY="${UPLOAD_KEY:-}"
UPLOAD_FLAG=--upload; [ -n "$PROOFAGENT_API_KEY" ] || { echo "No UPLOAD_KEY: running without upload"; UPLOAD_FLAG=; }
REASON_FLAG=; [ -n "${HARNESS_REASONING:-}" ] && [ "${HARNESS_REASONING}" != default ] && REASON_FLAG="--reasoning-effort ${HARNESS_REASONING}"
SEED_FLAG="--seed ${SEED:-42}"; [ "${SEED:-}" = none ] && SEED_FLAG="--seed -1"  # SEED=none: exploration (re-test + new attacks)
TURNS_FLAG=--adaptive-turns; [ -n "${TURNS:-}" ] && TURNS_FLAG="--turns $TURNS"
OUT="${OUT:-results/${AGENT}}"; mkdir -p "$OUT"
CTX="context/${AGENT}"; KN="$CTX/knowledge"
field() { ../../.venv/bin/python -c "import json,sys;print(json.load(open('../agents/$SPEC'))[sys.argv[1]])" "$1"; }
"${PROOF_BIN:-../../.venv/bin/proof}" run spec_agent.py --role "$(field role)" --business-case "$(field business_case)" --goal "$(field goal)" \
  --context-dir "$CTX" --domain-knowledge-dir "$KN" --assess-context --assess-compliance \
  --llm "${HARNESS_LLM:-lm_studio/prism-ml/bonsai-27b}" $REASON_FLAG $TURNS_FLAG $SEED_FLAG \
  --json "$OUT/run.json" --markdown "$OUT/run.md" --per "$OUT/run.per.json" \
  --agent "$AGENT" --agent-version "${AGENT_VERSION:-local}" $UPLOAD_FLAG --fail-on block --source manual --environment development ${EXTRA_FLAGS:-}
