#!/usr/bin/env bash
# All-local batch: Gemma 4 31B harness, Qwen3.5-9B agent, one agent after another. BATCH names the round.
cd "$(dirname "$0")"
BATCH="${BATCH:-r1}"; AGENTS="${AGENTS:-banking-dispute-agent medical-triage-assistant airline-rebooking-agent hr-recruiting-screener pharmacy-refill-assistant}"
for a in $AGENTS; do
  out="results/night/$BATCH/$a"; rm -rf "$out"; mkdir -p "$out"
  # LM Studio may reload a model on its own with its default (8k) context: make sure the harness model has 64k.
  if [[ "$HARNESS_LLM" == lm_studio/* ]]; then
    m="${HARNESS_LLM#lm_studio/}"
    if ! ~/.lmstudio/bin/lms ps 2>/dev/null | grep -E "^$m " | grep -q " 65536 "; then
      # LM Studio's load guard refuses the 64k harness model while the agent model is resident (it estimates
      # both KV caches), so free the agent model first and load it back after.
      ag="${AGENT_LLM#lm_studio/}"
      ~/.lmstudio/bin/lms unload "$m" >/dev/null 2>&1
      [[ "$AGENT_LLM" == lm_studio/* ]] && ~/.lmstudio/bin/lms unload "$ag" >/dev/null 2>&1
      sleep 5
      for try in 1 2 3; do
        ~/.lmstudio/bin/lms load "$m" --context-length 65536 --parallel 2 -y >/dev/null 2>&1
        ~/.lmstudio/bin/lms ps 2>/dev/null | grep -E "^$m " | grep -q " 65536 " && break
        sleep 20
      done
      [[ "$AGENT_LLM" == lm_studio/* ]] && ~/.lmstudio/bin/lms load "$ag" --context-length 32768 --parallel 2 -y >/dev/null 2>&1
      if ~/.lmstudio/bin/lms ps 2>/dev/null | grep -E "^$m " | grep -q " 65536 "; then
        echo "=== $(date +%H:%M) reloaded $m with 64k context"
      else
        echo "=== $(date +%H:%M) FAILED to load $m — skipping $a"; continue
      fi
    fi
  fi
  echo "=== $(date +%H:%M) START $a"
  UPLOAD_KEY= PROOFAGENT_API_KEY= AGENT="$a" HARNESS_LLM="${HARNESS_LLM:?}" AGENT_LLM="${AGENT_LLM:?}" \
    OUT="$out" AGENT_VERSION="night-$BATCH" ./run_full_agent.sh > "$out/log.txt" 2>&1
  echo "=== $(date +%H:%M) END $a rc=$? report=$([ -f "$out/run.md" ] && echo yes || echo no)"
done
echo "=== BATCH $BATCH DONE"
