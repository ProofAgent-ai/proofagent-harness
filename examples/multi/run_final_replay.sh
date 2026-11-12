#!/usr/bin/env bash
# Final pass: replay each tracker2 run (same stored conversation, no agent calls) with the FINAL harness code on
# eio-agents 0.9.0, through the real `proof run` path, upload to the local platform and verify.
# Usage: PROOF_BIN=<venv09>/bin/proof UPLOAD_KEY=... ./run_final_replay.sh
set -uo pipefail
cd "$(dirname "$0")"
ROOT=results/final; mkdir -p "$ROOT"
export LM_STUDIO_API_BASE=http://localhost:1234/v1 UPLOAD_URL="${UPLOAD_URL:-http://localhost:8000}"
export HARNESS_LLM="${HARNESS_LLM:-lm_studio/google/gemma-3-12b}" AGENT_LLM="${AGENT_LLM:-lm_studio/qwen3.5-9b-mlx}"
export TURNS=30 SEED=42 EXTRA_FLAGS= PROOFAGENT_RETEST=0   # no --fresh: the stored transcript is replayed
: "${UPLOAD_KEY:?set UPLOAD_KEY}"; : "${PROOF_BIN:?set PROOF_BIN to the eio 0.9 proof binary}"
AGENTS="${AGENTS:-property-leasing-agent code-generation-agent medical-triage-assistant hr-recruiting-screener airline-rebooking-agent}"
EIOV=$("${PROOF_BIN%/proof}/python" -c "import eio_agents;print(eio_agents.__version__)")
echo "=== FINAL REPLAY start $(date +%H:%M) eio $EIOV"
for agent in $AGENTS; do
  for rep in a b; do
    out="$ROOT/$agent/$rep"
    [ -f "$out/verify.json" ] && { echo "skip $agent/$rep"; continue; }
    mkdir -p "$out"; t0=$(date +%s)
    AGENT="$agent" OUT="$out" AGENT_VERSION="final-$rep" ./run_full_agent.sh > "$out/log.txt" 2>&1
    rc=$?
    "${PROOF_BIN%/proof}/python" tracker_verify.py "$out" "$agent" "$rep" > "$out/verify.log" 2>&1
    src=$(grep -o "reusing prior transcript\|fresh transcript" "$out/log.txt" | head -1)
    echo "REPLAY_DONE $agent/$rep rc=$rc $(( ($(date +%s)-t0)/60 ))min ${src:-?} $(date +%H:%M)"
  done
done
echo "=== FINAL REPLAY DONE $(date +%H:%M)"
