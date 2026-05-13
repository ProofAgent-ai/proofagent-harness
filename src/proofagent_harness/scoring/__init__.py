"""Scoring math — pure Python, deterministic, no LLM calls."""

from proofagent_harness.scoring.aggregator import (
    apply_certification,
    compute_final_score,
)

__all__ = ["apply_certification", "compute_final_score"]
