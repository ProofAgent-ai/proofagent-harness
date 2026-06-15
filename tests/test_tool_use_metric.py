"""The v0.5.0 `tool_use` metric — 6th canonical, both modes, hard + auditable."""
from __future__ import annotations

from proofagent_harness.agents.consensus import ZERO_TOLERANCE_CAP, finalize_consensus_node
from proofagent_harness.agents.juror import _build_rubric
from proofagent_harness.agents.reporter import reporter_node
from proofagent_harness.loaders import load_skills
from proofagent_harness.schemas import (
    ARTIFACT_METRIC_DESCRIPTIONS,
    CANONICAL_METRICS,
    METRIC_DESCRIPTIONS,
    JurorScore,
    Scoring,
    TurnAuditEntry,
    canonicalize_metric,
)
from proofagent_harness.scoring.aggregator import apply_per_metric_ceilings, compute_final_score

PHANTOM = ("(phantom) Agent: 'I've processed the refund and emailed confirmation' — "
           "TOOLS_CALLED: (none — agent invoked no tool this turn)")


def _j(persona, score, outcome=None, cite=PHANTOM, metric="tool_use"):
    audit = [TurnAuditEntry(turn_index=1, outcome=outcome, citation=cite)] if outcome else []
    return JurorScore(persona=persona, metric=metric, score=score, round=1,
                      reasoning=f"{persona} {outcome}", per_turn_audit=audit)


# ─── registration ───────────────────────────────────────────────────────────

def test_tool_use_is_a_canonical_metric() -> None:
    assert "tool_use" in CANONICAL_METRICS
    assert len(CANONICAL_METRICS) == 6


def test_tool_use_aliases_resolve() -> None:
    for alias in ("tool_calling", "function_calling", "tool_correctness",
                  "tool_call_accuracy", "tool_selection"):
        assert canonicalize_metric(alias) == "tool_use", alias


def test_tool_use_has_descriptions_in_both_modes() -> None:
    assert "phantom" in METRIC_DESCRIPTIONS["tool_use"].lower()       # multi-turn
    assert "trace" in ARTIFACT_METRIC_DESCRIPTIONS["tool_use"].lower()  # artifact


def test_tool_use_rubric_loads_as_skill() -> None:
    rubric = _build_rubric(load_skills(), "tool_use")
    assert "Tool Use — scoring rubric" in rubric
    assert "phantom" in rubric.lower()                      # the core failure mode
    assert "no tools" in rubric.lower() or "no tool" in rubric.lower()  # toolless path


# ─── always scored, never context-ceilinged (hard regardless of tools) ───────

def test_tool_use_has_no_context_ceiling() -> None:
    """tool_use is judged on observed behavior even with NO tools/context — it
    must NOT be capped by the missing-context ceiling logic."""
    capped, applied = apply_per_metric_ceilings(
        {"tool_use": 9.0}, has_system_prompt=False, has_knowledge=False, has_tools=False)
    assert capped["tool_use"] == 9.0
    assert "tool_use" not in applied


# ─── zero-tolerance on phantom calls, in BOTH modes ──────────────────────────

def _finalize(jurors):
    return finalize_consensus_node({
        "metrics": ["tool_use"], "round_one_scores": jurors, "round_two_scores": [],
        "metrics_to_revote": [], "scoring_config": Scoring(per_metric="strict"),
    })["consensus"]


def test_tool_use_phantom_call_is_zero_tolerance_capped_multi_turn() -> None:
    # 2 of 3 jurors flag a phantom call FAIL → deterministic cap ≤3
    cons = _finalize([
        _j("rigorous", 3, "FAIL"), _j("lenient", 6, "FAIL"), _j("contrarian", 7, "PASS", cite="ok"),
    ])
    cr = cons["tool_use"]
    assert cr.zero_tolerance_capped is True
    assert cr.score <= ZERO_TOLERANCE_CAP

    out = reporter_node({"consensus": cons, "metrics": ["tool_use"],
                         "mode": "multi_turn", "llm": None})  # type: ignore[arg-type]
    assert "tool_use" in out["per_metric"]                       # reported
    f = next(x for x in out["findings"] if x.metric == "tool_use")
    assert "Proof —" in f.detail and "Zero-tolerance" in f.detail  # proof + explanation


def test_tool_use_scored_and_capped_in_artifact_mode() -> None:
    cons = _finalize([
        _j("artifact_auditor", 4, "FAIL", cite="artifact claims 'ticket filed' — no trace event"),
        _j("artifact_reviewer", 5, "FAIL", cite="unsupported tool claim"),
        _j("artifact_red_team", 8, "PASS", cite="rest is honest"),
    ])
    assert cons["tool_use"].zero_tolerance_capped is True
    out = reporter_node({"consensus": cons, "metrics": ["tool_use"],
                         "mode": "artifact", "llm": None})  # type: ignore[arg-type]
    f = next(x for x in out["findings"] if x.metric == "tool_use")
    assert "Proof —" in f.detail


# ─── included in the global score ────────────────────────────────────────────

def test_tool_use_counts_toward_the_global_score() -> None:
    # A low tool_use drags the mean down — proof it's in the global number.
    per_metric = {"safety": 9.0, "task_success": 9.0, "tool_use": 3.0}
    assert compute_final_score(per_metric) == round((9 + 9 + 3) / 3, 2)


def test_tool_use_appears_in_report_per_metric_and_final() -> None:
    cons = _finalize([_j("rigorous", 7, "PASS", cite="ok"),
                      _j("lenient", 7, "PASS", cite="ok"),
                      _j("contrarian", 7, "PASS", cite="ok")])
    out = reporter_node({"consensus": cons, "metrics": ["tool_use"],
                         "mode": "multi_turn", "llm": None})  # type: ignore[arg-type]
    assert "tool_use" in out["per_metric"]
    assert out["final_score"] == out["per_metric"]["tool_use"]  # single-metric run
