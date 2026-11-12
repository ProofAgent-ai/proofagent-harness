#!/usr/bin/env bash
# Evaluate one production-style spec agent with the LOCAL harness LLM (LM Studio) and upload the PER.
# usage: run_spec_local.sh <spec.json> <agent-name> [turns]
#   upload target/key: UPLOAD_URL / UPLOAD_KEY if set, else PROOFAGENT_API_BASE_URL / PROOFAGENT_API_KEY from ../../.env
set -euo pipefail
cd "$(dirname "$0")"
_KEY="${UPLOAD_KEY:-}"; _URL="${UPLOAD_URL:-}"
set -a; . ../../.env; set +a
[ -n "$_KEY" ] && export PROOFAGENT_API_KEY="$_KEY"
[ -n "$_URL" ] && export PROOFAGENT_API_BASE_URL="$_URL"
export SPEC="$1" AGENT_LLM=gpt-5-nano AGENT_REASONING_EFFORT=low LITELLM_LOG=ERROR LM_STUDIO_API_BASE=http://localhost:1234/v1
NAME="$2"; TURNS="${3:-10}"; OUT="results/$NAME/local"; mkdir -p "$OUT"
field() { ../../.venv/bin/python -c "import json,sys;print(json.load(open('../agents/$SPEC'))[sys.argv[1]])" "$1"; }
echo "== $NAME ($SPEC) · $TURNS turns · jury lm_studio/google/gemma-4-26b-a4b-qat · upload $PROOFAGENT_API_BASE_URL"
../../.venv/bin/proof run spec_agent.py --role "$(field role)" --business-case "$(field business_case)" --goal "$(field goal)" \
  --llm lm_studio/google/gemma-4-26b-a4b-qat --turns "$TURNS" --seed 42 \
  --context-dir "context/$NAME" --domain-knowledge-dir "context/$NAME/knowledge" --assess-context --assess-compliance \
  --json "$OUT/run.json" --markdown "$OUT/run.md" --per "$OUT/run.per.json" \
  --agent "$NAME" --agent-version local-gemma --upload --fail-on block --source manual --environment development
