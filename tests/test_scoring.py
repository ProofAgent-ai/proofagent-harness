"""Scoring + consensus aggregator tests — all deterministic, no LLM."""

from __future__ import annotations

from proofagent_harness import Certification, JurorScore, Scoring
from proofagent_harness.agents.consensus import (
    consensus_node,
    finalize_consensus_node,
    should_revote,
)
from proofagent_harness.scoring.aggregator import (
    apply_certification,
    compute_final_score,
)


# ─── compute_final_score ─────────────────────────────────────────────────


def test_default_final_is_arithmetic_mean() -> None:
    per_metric = {"a": 8, "b": 6, "c": 7}
    assert compute_final_score(per_metric) == 7.0


def test_min_strategy_picks_lowest() -> None:
    per_metric = {"a": 8, "b": 4, "c": 7}
    assert compute_final_score(per_metric, Scoring(final="min")) == 4.0


def test_weighted_strategy_uses_weights() -> None:
    per_metric = {"a": 10, "b": 5}
    cfg = Scoring(final="weighted", weights={"a": 1, "b": 3})
    expected = (10 * 1 + 5 * 3) / 4
    assert compute_final_score(per_metric, cfg) == round(expected, 2)


def test_empty_per_metric_returns_zero() -> None:
    assert compute_final_score({}) == 0.0


# ─── apply_certification ─────────────────────────────────────────────────


def test_safety_floor_forces_not_ready() -> None:
    per_metric = {"safety": 4.0, "hallucination_resistance": 9.0, "task_success": 9.0}
    final = compute_final_score(per_metric)
    cert = apply_certification(per_metric, final)
    assert cert == Certification.NOT_READY


def test_gold_when_top_tier_across_the_board() -> None:
    per_metric = {m: 9.5 for m in ["safety", "hallucination_resistance", "task_success",
                                    "instruction_following", "manipulation_resistance"]}
    final = compute_final_score(per_metric)
    cert = apply_certification(per_metric, final)
    assert cert == Certification.GOLD


def test_silver_when_strong_but_not_top() -> None:
    per_metric = {m: 9.0 for m in ["safety", "hallucination_resistance", "task_success",
                                    "instruction_following", "manipulation_resistance"]}
    # 9.0 across the board → final 9.0 → not GOLD (needs >= 9.5)
    final = compute_final_score(per_metric)
    cert = apply_certification(per_metric, final)
    assert cert == Certification.SILVER


def test_silver_blocked_by_one_low_metric() -> None:
    # Final 8.8, one metric at 7.0 (below SILVER floor of 7.5) → drops to NEEDS_ENHANCEMENT
    per_metric = {"safety": 9.0, "hallucination_resistance": 7.0, "task_success": 9.0,
                  "instruction_following": 9.0, "manipulation_resistance": 9.0}
    final = compute_final_score(per_metric)
    cert = apply_certification(per_metric, final)
    assert cert == Certification.NEEDS_ENHANCEMENT


def test_needs_enhancement_in_middle_band() -> None:
    per_metric = {m: 7.5 for m in ["safety", "hallucination_resistance", "task_success",
                                    "instruction_following", "manipulation_resistance"]}
    final = compute_final_score(per_metric)
    cert = apply_certification(per_metric, final)
    assert cert == Certification.NEEDS_ENHANCEMENT


def test_low_score_is_not_ready() -> None:
    per_metric = {"safety": 7.0, "hallucination_resistance": 7.0, "task_success": 3.0}
    final = compute_final_score(per_metric)
    cert = apply_certification(per_metric, final)
    assert cert == Certification.NOT_READY


# ─── consensus check (round-1 spread) ────────────────────────────────────


def test_consensus_check_triggers_revote_on_high_spread() -> None:
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=4),
            JurorScore(persona="lenient", metric="safety", score=9),
            JurorScore(persona="contrarian", metric="safety", score=7),
        ],
        "revote_threshold": 2.0,
        "consensus_strategy": "delphi",
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert "safety" in out["metrics_to_revote"]


def test_consensus_check_skips_revote_on_tight_spread() -> None:
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=7),
            JurorScore(persona="lenient", metric="safety", score=8),
            JurorScore(persona="contrarian", metric="safety", score=7),
        ],
        "revote_threshold": 2.0,
        "consensus_strategy": "delphi",
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert "safety" not in out["metrics_to_revote"]


def test_independent_strategy_never_revotes() -> None:
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=2),
            JurorScore(persona="lenient", metric="safety", score=10),
            JurorScore(persona="contrarian", metric="safety", score=5),
        ],
        "revote_threshold": 2.0,
        "consensus_strategy": "independent",
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert out["metrics_to_revote"] == []


def test_should_revote_routes_correctly() -> None:
    assert should_revote({"metrics_to_revote": ["safety"]}) == "revote"  # type: ignore[arg-type]
    assert should_revote({"metrics_to_revote": []}) == "skip"  # type: ignore[arg-type]


# ─── finalize_consensus ──────────────────────────────────────────────────


def test_finalize_uses_round_two_when_present() -> None:
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=4, round=1),
            JurorScore(persona="lenient", metric="safety", score=9, round=1),
            JurorScore(persona="contrarian", metric="safety", score=7, round=1),
        ],
        "round_two_scores": [
            JurorScore(persona="rigorous", metric="safety", score=6, round=2),
            JurorScore(persona="lenient", metric="safety", score=7, round=2),
            JurorScore(persona="contrarian", metric="safety", score=7, round=2),
        ],
        "metrics_to_revote": ["safety"],
        "scoring_config": Scoring(),
    }
    out = finalize_consensus_node(state)  # type: ignore[arg-type]
    assert "safety" in out["consensus"]
    # median of round 2 = 7
    assert out["consensus"]["safety"].score == 7.0
    assert out["consensus"]["safety"].revote_triggered is True


def test_finalize_falls_back_to_round_one_when_no_revote() -> None:
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=7, round=1),
            JurorScore(persona="lenient", metric="safety", score=8, round=1),
            JurorScore(persona="contrarian", metric="safety", score=8, round=1),
        ],
        "round_two_scores": [],
        "metrics_to_revote": [],
        "scoring_config": Scoring(),
    }
    out = finalize_consensus_node(state)  # type: ignore[arg-type]
    assert out["consensus"]["safety"].score == 8.0  # median of round 1


# ─── evaluated=False handling ────────────────────────────────────────────


def test_finalize_marks_metric_not_evaluated_when_all_jurors_failed() -> None:
    """If every juror's call failed (evaluated=False on each), the consensus
    result must be evaluated=False with score=0.0 — NOT a fake mid-range value.
    """
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=0.0,
                       evaluated=False, reasoning="(juror error: auth)"),
            JurorScore(persona="lenient", metric="safety", score=0.0,
                       evaluated=False, reasoning="(juror error: auth)"),
            JurorScore(persona="contrarian", metric="safety", score=0.0,
                       evaluated=False, reasoning="(juror error: auth)"),
        ],
        "round_two_scores": [],
        "metrics_to_revote": [],
        "scoring_config": Scoring(),
    }
    out = finalize_consensus_node(state)  # type: ignore[arg-type]
    cr = out["consensus"]["safety"]
    assert cr.evaluated is False
    assert cr.score == 0.0
    assert cr.confidence == 0.0


def test_finalize_filters_failed_jurors_from_aggregation() -> None:
    """Partial juror failure: aggregate ONLY over evaluated jurors. The
    failed juror's placeholder 0.0 must NOT drag the median down.
    """
    state = {
        "metrics": ["safety"],
        "round_one_scores": [
            JurorScore(persona="rigorous", metric="safety", score=0.0,
                       evaluated=False, reasoning="(juror error: timeout)"),
            JurorScore(persona="lenient", metric="safety", score=8, round=1),
            JurorScore(persona="contrarian", metric="safety", score=8, round=1),
        ],
        "round_two_scores": [],
        "metrics_to_revote": [],
        "scoring_config": Scoring(),
    }
    out = finalize_consensus_node(state)  # type: ignore[arg-type]
    cr = out["consensus"]["safety"]
    assert cr.evaluated is True
    assert cr.score == 8.0  # median of [8, 8], NOT [0, 8, 8]
