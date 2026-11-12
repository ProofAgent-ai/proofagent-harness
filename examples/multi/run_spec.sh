#!/usr/bin/env bash
# usage: run_spec.sh <spec.json> <agent-name>
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
export SPEC="$1" AGENT_LLM=gpt-5-nano AGENT_REASONING_EFFORT=low LITELLM_LOG=ERROR
NAME="$2"; OUT="results/$NAME"; mkdir -p "$OUT"
ROLE=$(../../.venv/bin/python -c "import json;d=json.load(open('../agents/$1'));print(d['role'])")
CASE=$(../../.venv/bin/python -c "import json;d=json.load(open('../agents/$1'));print(d['business_case'])")
GOAL=$(../../.venv/bin/python -c "import json;d=json.load(open('../agents/$1'));print(d['goal'])")
../../.venv/bin/proof run spec_agent.py --role "$ROLE" --business-case "$CASE" --goal "$GOAL" \
  --llm gpt-5-nano --reasoning-effort low --turns 20 --fresh --seed -1 --consensus debate \
  --context-dir "context/$NAME" --domain-knowledge-dir "context/$NAME/knowledge" --assess-context --assess-compliance --json "$OUT/e2e.json" --per "$OUT/e2e.per.json" \
  --agent "$NAME" --agent-version e2e-v2-context --upload --upload-transcript --fail-on block --source manual \
  > "$OUT/run.log" 2>&1 || true
echo "EXIT done" >> "$OUT/run.log"
