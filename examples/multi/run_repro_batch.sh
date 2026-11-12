#!/usr/bin/env bash
# Reproducibility batch: every spec agent evaluated twice with the SAME seed, both LIVE (--fresh: no transcript
# reuse), so any difference between A and B is run-to-run variance (attacker wording, agent, jury), not replay.
# Local models only. Usage: ./run_repro_batch.sh [batch-name]   (env: TURNS, HARNESS_LLM, AGENT_LLM)
set -uo pipefail
cd "$(dirname "$0")"
B="${1:-repro}"; ROOT="results/$B"; mkdir -p "$ROOT"
export LM_STUDIO_API_BASE=http://localhost:1234/v1
export HARNESS_LLM="${HARNESS_LLM:-lm_studio/google/gemma-3-12b}" AGENT_LLM="${AGENT_LLM:-lm_studio/qwen3.5-9b-mlx}"
export TURNS="${TURNS:-20}" SEED=42 EXTRA_FLAGS=--fresh PROOFAGENT_RETEST=0
LMS=~/.lmstudio/bin/lms
H="${HARNESS_LLM#lm_studio/}"; A="${AGENT_LLM#lm_studio/}"
guard() {  # both models loaded at the right context, or reload them (LM Studio unloads idle models at 8k)
  for i in 1 2 3; do
    ctx=$(curl -s "localhost:1234/api/v0/models/$H" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('loaded_context_length') or 0)" 2>/dev/null || echo 0)
    st=$(curl -s "localhost:1234/api/v0/models/$A" | python3 -c "import json,sys;print(json.load(sys.stdin).get('state'))" 2>/dev/null)
    [ "${ctx:-0}" -ge 65536 ] && [ "$st" = loaded ] && return 0
    echo "  guard: reloading models (harness ctx=$ctx, agent=$st)"
    $LMS unload --all >/dev/null 2>&1
    $LMS load "$H" --context-length 65536 --parallel 2 -y >/dev/null 2>&1
    $LMS load "$A" --context-length 32768 --parallel 2 -y >/dev/null 2>&1
    sleep 5
  done
  return 1
}
echo "=== BATCH $B start $(date +%H:%M) harness=$HARNESS_LLM agent=$AGENT_LLM turns=$TURNS"
for spec in ../agents/*.json; do
  agent=$(basename "$spec" .json); agent=${agent//_/-}
  for rep in a b; do
    out="$ROOT/$agent/$rep"
    [ -f "$out/run.json" ] && { echo "skip $agent/$rep (done)"; continue; }
    guard || { echo "=== $agent/$rep FAILED: models would not load"; continue; }
    mkdir -p "$out"; t0=$(date +%s)
    AGENT="$agent" OUT="$out" AGENT_VERSION="$B-$rep" ./run_full_agent.sh > "$out/log.txt" 2>&1
    rc=$?; [ -f "$out/run.json" ] || echo "=== $agent/$rep FAILED rc=$rc (see $out/log.txt)"
    echo "$(date +%H:%M) $agent/$rep rc=$rc $(( ($(date +%s)-t0)/60 ))min"
  done
done
echo "=== BATCH $B DONE $(date +%H:%M)"
