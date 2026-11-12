#!/usr/bin/env bash
# Full test: medical triage agent on a LOCAL model (default Bonsai 27B) (LM Studio); harness LLM gpt-5-nano (reasoning: HARNESS_REASONING, default low).
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
export SPEC=medical_triage_assistant.json AGENT_LLM="${AGENT_LLM:-lm_studio/prism-ml/bonsai-27b}" LM_STUDIO_API_BASE=http://localhost:1234/v1 LITELLM_LOG=ERROR
export PROOFAGENT_API_BASE_URL=https://app.proofagent.ai PROOFAGENT_API_KEY="${UPLOAD_KEY:-${PROOFAGENT_API_KEY:-}}"
UPLOAD_FLAG=--upload; [ -n "$PROOFAGENT_API_KEY" ] || { echo "No UPLOAD_KEY: running without upload"; UPLOAD_FLAG=; }
REASON_FLAG="--reasoning-effort ${HARNESS_REASONING:-low}"; [ "${HARNESS_REASONING:-}" = default ] && REASON_FLAG=
mkdir -p "${OUT:-results/full_medical}"
field() { ../../.venv/bin/python -c "import json,sys;print(json.load(open('../agents/$SPEC'))[sys.argv[1]])" "$1"; }
"${PROOF_BIN:-../../.venv/bin/proof}" run spec_agent.py --role "$(field role)" --business-case "$(field business_case)" --goal "$(field goal)" \
  --context-dir context/medical-triage-assistant --domain-knowledge-dir context/medical-triage-assistant/knowledge \
  --assess-context --assess-compliance \
  --llm "${HARNESS_LLM:-gpt-5-nano}" $REASON_FLAG --adaptive-turns --seed 42 \
  --json ${OUT:-results/full_medical}/run.json --markdown ${OUT:-results/full_medical}/run.md --per ${OUT:-results/full_medical}/run.per.json \
  --agent medical-triage-assistant --agent-version "${AGENT_VERSION:-nano-judge-bonsai-agent}" $UPLOAD_FLAG --fail-on block --source manual --environment development
