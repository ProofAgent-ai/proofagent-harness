#!/usr/bin/env bash
#
# Artifact evaluation — grade a finished credit decision report against ground truth.
#
#   bash run_artifact.sh            # local
#   bash run_artifact.sh --upload   # + push to the governance dashboard and gate
#
# The report in artifact/credit_decision_report.md contains deliberate, realistic issues
# (an over-policy limit, an out-of-range APR, an unsupported claim, a fair-lending rationale,
# a vague adverse-action reason) so the evaluation surfaces concrete, evidence-linked findings.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p results

proof artifact artifact/credit_decision_report.md \
  --type report \
  --domain-knowledge-dir artifact/corpus \
  --assess-context \
  --llm gpt-4.1 \
  --fallback-llm anthropic/claude-haiku-4-5 \
  --consensus debate \
  --seed 42 \
  --json results/credit_report.json \
  --markdown results/credit_report.md \
  --agent credit-allocation-agent \
  --agent-version "$(git rev-parse --short HEAD 2>/dev/null || echo example)" \
  --profile artifact_governance_default \
  --fail-on block \
  --source manual \
  "$@"
