#!/usr/bin/env bash
# One evaluation per domain, adaptive turn count, local Gemma jury, every PER uploaded; phase timings recorded.
# usage: UPLOAD_KEY=... UPLOAD_URL=... ./run_domains.sh
set -uo pipefail
cd "$(dirname "$0")"
_KEY="${UPLOAD_KEY:-}"; _URL="${UPLOAD_URL:-}"
set -a; . ../../.env; set +a
[ -n "$_KEY" ] && export PROOFAGENT_API_KEY="$_KEY"
[ -n "$_URL" ] && export PROOFAGENT_API_BASE_URL="$_URL"
export AGENT_LLM=gpt-5-nano AGENT_REASONING_EFFORT=low LITELLM_LOG=ERROR LM_STUDIO_API_BASE=http://localhost:1234/v1 PROOFAGENT_JURY_CONCURRENCY="${PROOFAGENT_JURY_CONCURRENCY:-2}"
BATCH="${BATCH:-results/domains}"; mkdir -p "$BATCH"
JURY="${JURY_LLM:-lm_studio/qwen/qwen3.8-27b}"
VERSION="${AGENT_VERSION:-local-qwen}"
AGENTS=(
  "airline_rebooking_agent.json airline-rebooking-agent"
  "code_generation_agent.json code-generation-agent"
  "customer_support_agent.json customer-support-agent"
  "financial_advisor_agent.json financial-advisor-agent"
  "hr_recruiting_screener.json hr-recruiting-screener"
  "insurance_claims_agent.json insurance-claims-agent"
  "legal_intake_assistant.json legal-intake-assistant"
  "medical_triage_assistant.json medical-triage-assistant"
  "privacy_security_agent.json privacy-security-agent"
  "telecom_account_agent.json telecom-account-agent"
)
if [ -n "${AGENT_LIST:-}" ]; then IFS=";" read -r -a AGENTS <<< "$AGENT_LIST"; fi
if [ -n "${AGENT_FILE:-}" ]; then AGENTS=(); while IFS= read -r ln; do [ -n "$ln" ] && AGENTS+=("$ln"); done < "$AGENT_FILE"; fi
for pair in "${AGENTS[@]}"; do
  read -r SPEC NAME <<< "$pair"
  export SPEC
  OUT="$BATCH/$NAME"; mkdir -p "$OUT"
  field() { ../../.venv/bin/python -c "import json,sys;print(json.load(open('../agents/$SPEC'))[sys.argv[1]])" "$1"; }
  echo "===== $NAME ($SPEC) — $(date +%H:%M:%S)"
  ../../.venv/bin/python phase_timer.py "$OUT/phases.json" "$OUT/run.log" -- \
    ../../.venv/bin/proof run spec_agent.py --role "$(field role)" --business-case "$(field business_case)" --goal "$(field goal)" \
      --llm "$JURY" --adaptive-turns --seed 42 \
      --context-dir "context/$NAME" --domain-knowledge-dir "context/$NAME/knowledge" --assess-context --assess-compliance \
      --json "$OUT/run.json" --markdown "$OUT/run.md" --per "$OUT/run.per.json" \
      --agent "$NAME" --agent-version "$VERSION" --upload --fail-on block --source manual --environment development \
    > /dev/null
  echo "      done $(date +%H:%M:%S): $(cat "$OUT/phases.json" | tr -d '\n ' | cut -c1-200)"
done
echo "===== ALL DONE $(date +%H:%M:%S)"
