#!/usr/bin/env bash
# Tracker batch: 10 agents x runs A and B (same seed, both live), 30 turns, every PER uploaded to the LOCAL platform and
# verified (tracker_verify.py). After each run prints "RUN_DONE <agent>/<rep>" so the tracker can be refreshed.
# Usage: UPLOAD_KEY=... ./run_tracker_batch.sh [batch-name]
set -uo pipefail
cd "$(dirname "$0")"
B="${1:-tracker}"; ROOT="results/$B"; mkdir -p "$ROOT"
export LM_STUDIO_API_BASE=http://localhost:1234/v1 UPLOAD_URL="${UPLOAD_URL:-http://localhost:8000}"
export HARNESS_LLM="${HARNESS_LLM:-lm_studio/google/gemma-3-12b}" AGENT_LLM="${AGENT_LLM:-lm_studio/qwen3.5-9b-mlx}"
export TURNS="${TURNS:-30}" SEED=42 EXTRA_FLAGS=--fresh PROOFAGENT_RETEST=0
: "${UPLOAD_KEY:?set UPLOAD_KEY (local platform key)}"
AGENTS="${AGENTS:-customer-support-agent banking-dispute-agent medical-triage-assistant hotel-reservations-agent utility-billing-agent property-leasing-agent privacy-security-agent airline-rebooking-agent hr-recruiting-screener code-generation-agent}"
LMS=~/.lmstudio/bin/lms
H="${HARNESS_LLM#lm_studio/}"; A="${AGENT_LLM#lm_studio/}"
guard() {
  for i in 1 2 3; do
    ctx=$(curl -s "localhost:1234/api/v0/models/$H" | python3 -c "import json,sys;print(json.load(sys.stdin).get('loaded_context_length') or 0)" 2>/dev/null || echo 0)
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
status() { printf '{"current":"%s","started":"%s","state":"%s"}\n' "$1" "$(date -u +%FT%TZ)" "$2" > "$ROOT/status.json"; }
echo "=== BATCH $B start $(date +%H:%M) turns=$TURNS"
for agent in $AGENTS; do
  for rep in a b; do
    out="$ROOT/$agent/$rep"
    [ -f "$out/verify.json" ] && { echo "skip $agent/$rep"; continue; }
    status "$agent/$rep" running
    guard || { echo "RUN_DONE $agent/$rep FAILED models"; continue; }
    mkdir -p "$out"; t0=$(date +%s)
    AGENT="$agent" OUT="$out" AGENT_VERSION="$B-$rep" ./run_full_agent.sh > "$out/log.txt" 2>&1
    rc=$?
    ../../.venv/bin/python tracker_verify.py "$out" "$agent" "$rep" > "$out/verify.log" 2>&1
    echo "RUN_DONE $agent/$rep rc=$rc $(( ($(date +%s)-t0)/60 ))min $(date +%H:%M)"
  done
done
status "" done
echo "=== BATCH $B DONE $(date +%H:%M)"
