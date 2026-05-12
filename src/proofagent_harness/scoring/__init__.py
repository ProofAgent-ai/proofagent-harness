"""Scoring math — pure Python, deterministic, no LLM calls."""

from proofagent_harness.scoring.aggregator import (
    apply_certification,
    compute_final_score,
)

__all__ = ["compute_final_score", "apply_certification"]
