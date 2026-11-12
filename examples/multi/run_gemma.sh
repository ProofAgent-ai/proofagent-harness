#!/usr/bin/env bash
# usage: run_gemma.sh <spec.json>   — harness and agent both on local Gemma (LM Studio)
set -uo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
SPEC_FILE="$1"; NAME=$(basename "$SPEC_FILE" .json | tr '_' '-')
export SPEC="$SPEC_FILE" AGENT_LLM=lm_studio/gemma-4-e4b-it-mlx LM_STUDIO_API_BASE=http://localhost:1234/v1 \
       PROOFAGENT_JURY_CONCURRENCY=4 LITELLM_LOG=ERROR
OUT="gemma_results/$NAME"; mkdir -p "$OUT"
../../.venv/bin/proof run spec_agent.py --context-dir "context/$NAME" --domain-knowledge-dir "context/$NAME/knowledge" \
  --assess-context --assess-compliance --llm lm_studio/google/gemma-4-26b-a4b-qat \
  --turns 15 --fresh --seed -1 --consensus debate \
  --json "$OUT/e2e.json" --per "$OUT/e2e.per.json" \
  --agent "$NAME" --agent-version gemma-v1 --upload --upload-transcript --fail-on block --source manual \
  > "$OUT/run.log" 2>&1
echo "EXIT $?" >> "$OUT/run.log"
