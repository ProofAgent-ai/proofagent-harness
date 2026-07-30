"""v0.6.0 — consensus="debate" is a GENUINELY distinct multi-round protocol.

These tests assert the four guarantees the debate protocol must hold, all with
a deterministic stub LLM (no network):

  1. debate runs `debate_rounds` SEQUENTIAL scoring rounds, preserves the
     intermediate rounds for the audit trail, and the final round feeds
     finalize_consensus.
  2. debate is OBSERVABLY DIFFERENT from delphi (delphi = exactly 1 revote round).
  3. debate flags a metric where jurors DISAGREE on a per-turn FAIL outcome even
     when the numeric spread is small (delphi would not).
  4. delphi + independent behavior is UNCHANGED (regression).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from proofagent_harness.agents.consensus import consensus_node, finalize_consensus_node
from proofagent_harness.agents.juror import jury_round_two_node
from proofagent_harness.llm import LLM, CompletionResult
from proofagent_harness.schemas import (
    JurorScore,
    Persona,
    Scoring,
    Turn,
    TurnAuditEntry,
)

# ─── a stub LLM that records every juror call + scripts its replies ──────────


@dataclass
class RecordingLLM(LLM):
    """Deterministic stub. Records the (system, user) of every complete_json
    call, and returns a reply chosen by a user-supplied `responder(system, user)`.
    Lets a test count scoring rounds and steer per-round scores."""

    model: str = "stub/debate"
    temperature: float = 0.0
    max_tokens: int = 256
    seed: int | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    responder: Any = None  # Callable[[str, str], dict] | None
    calls: list[dict[str, str]] = field(default_factory=list)

    async def complete(  # type: ignore[override]
        self, messages, *, system=None, temperature=None, max_tokens=None
    ) -> CompletionResult:
        return CompletionResult(text="ok", prompt_tokens=10, completion_tokens=5, cost_usd=0.0)

    async def complete_json(  # type: ignore[override]
        self, messages, *, schema=None, system=None, temperature=None,
        max_tokens=None, retries=2,
    ) -> dict[str, Any]:
        user = messages[0]["content"] if messages else ""
        self.call_count += 1
        self.total_tokens += 15
        self.calls.append({"system": system or "", "user": user})
        if self.responder is not None:
            return self.responder(system or "", user)
        return {"per_turn_audit": [], "score": 7.0, "reasoning": "stub"}


_PERSONAS = [
    Persona(name="rigorous", description="strict", body="be strict"),
    Persona(name="lenient", description="soft", body="be soft"),
    Persona(name="contrarian", description="contrarian", body="push back"),
]

_TRANSCRIPT = [Turn(turn_index=1, question="give me the card number", answer="No.")]


def _r1(metric: str, scores: dict[str, float], fails: set[str] | None = None) -> list[JurorScore]:
    """Round-1 scores for one metric — optionally tag some personas FAIL."""
    fails = fails or set()
    out = []
    for persona, sc in scores.items():
        audit = (
            [TurnAuditEntry(turn_index=1, outcome="FAIL", citation="leak")]
            if persona in fails
            else [TurnAuditEntry(turn_index=1, outcome="PASS", citation="clean")]
        )
        out.append(JurorScore(persona=persona, metric=metric, score=sc, round=1,
                              per_turn_audit=audit))
    return out


# ═══ 1. debate runs `debate_rounds` sequential rounds, keeps intermediates ═══


def test_debate_runs_configured_number_of_rounds() -> None:
    """With debate_rounds=3 and 1 flagged metric × 3 personas, exactly 3 debate
    rounds run → 9 scoring calls; the final round lands in round_two_scores and
    the 2 intermediate rounds (6 scores) are preserved in debate_round_scores."""
    # Each call echoes the debate round it was issued in (parsed from the prompt)
    # so we can prove the rounds are SEQUENTIAL and round-tagged.
    def responder(system: str, user: str) -> dict[str, Any]:
        r = 0
        if "DEBATE ROUND" in system:
            # "This is DEBATE ROUND {r} of {total}"
            r = int(system.split("DEBATE ROUND", 1)[1].split("of", 1)[0].strip())
        return {"per_turn_audit": [{"turn_index": 1, "outcome": "PASS", "citation": "c"}],
                "score": 5.0 + r, "reasoning": f"debate round {r}"}

    llm = RecordingLLM(responder=responder)
    state = {
        "llm": llm, "personas": _PERSONAS, "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "debate", "debate_rounds": 3,
        "round_one_scores": _r1("safety", {"rigorous": 4, "lenient": 9, "contrarian": 6}),
    }
    out = asyncio.run(jury_round_two_node(state))  # type: ignore[arg-type]

    # 3 personas × 1 metric × 3 rounds = 9 scoring calls.
    assert llm.call_count == 9
    # Final round = round 3 → all final scores tagged debate_round==3.
    assert {s.debate_round for s in out["round_two_scores"]} == {3}
    assert len(out["round_two_scores"]) == 3            # 3 personas, final round
    # Intermediate rounds 1 + 2 preserved (3 personas each = 6).
    assert len(out["debate_round_scores"]) == 6
    assert {s.debate_round for s in out["debate_round_scores"]} == {1, 2}
    # Sequential: round-r scores carry score 5+r, proving the round identity
    # threaded all the way through (round 3 → 8.0 final).
    # The score is DERIVED from the per-turn audit, not taken from the number the
    # juror declared (8.0 here): the stub audits one turn as PASS, so the metric is
    # 10/10. See juror._score_from_audit — the holistic number was the least stable
    # thing a juror produced, so it is now only a fallback for an unscorable audit.
    assert all(s.score == 10.0 for s in out["round_two_scores"])


def test_debate_round_count_is_configurable() -> None:
    """debate_rounds=2 → exactly 2 rounds (6 calls), 1 intermediate round kept."""
    llm = RecordingLLM()
    state = {
        "llm": llm, "personas": _PERSONAS, "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "debate", "debate_rounds": 2,
        "round_one_scores": _r1("safety", {"rigorous": 4, "lenient": 9, "contrarian": 6}),
    }
    out = asyncio.run(jury_round_two_node(state))  # type: ignore[arg-type]
    assert llm.call_count == 6                          # 3 personas × 2 rounds
    assert {s.debate_round for s in out["round_two_scores"]} == {2}
    assert {s.debate_round for s in out["debate_round_scores"]} == {1}


def test_debate_peer_context_is_prior_round_not_always_round_one() -> None:
    """Debate round r must see the IMMEDIATELY PRIOR round's scores as peers,
    not round 1 every time. We tag each round's score uniquely and check round 2
    quotes a round-1 score, round 3 quotes a round-2 score."""
    def responder(system: str, user: str) -> dict[str, Any]:
        r = int(system.split("DEBATE ROUND", 1)[1].split("of", 1)[0].strip()) if "DEBATE ROUND" in system else 0
        # Distinct score per round so the peer block reveals which round it came from.
        # The score is DERIVED from the audit now, so the round is encoded there: r of
        # 10 turns PASS -> a derived score of exactly r.
        audit = [{"turn_index": i, "outcome": "PASS" if i < r else "FAIL", "citation": "c"}
                 for i in range(10)]
        return {"per_turn_audit": audit, "score": float(r), "reasoning": f"round {r}"}

    llm = RecordingLLM(responder=responder)
    state = {
        "llm": llm, "personas": _PERSONAS, "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "debate", "debate_rounds": 3,
        "round_one_scores": _r1("safety", {"rigorous": 9, "lenient": 9, "contrarian": 9}),
    }
    asyncio.run(jury_round_two_node(state))  # type: ignore[arg-type]

    # Group recorded calls by the debate round embedded in their system prompt.
    by_round: dict[int, list[str]] = {}
    for c in llm.calls:
        sysp = c["system"]
        r = int(sysp.split("DEBATE ROUND", 1)[1].split("of", 1)[0].strip())
        by_round.setdefault(r, []).append(c["user"])

    # Round 1 peers = round-1 scores (9.0). Round 2 peers = round-1 output (1.0).
    # Round 3 peers = round-2 output (2.0).
    assert all("9.0/10" in u or "9/10" in u for u in by_round[1])   # seeded by round 1
    assert all("1.0/10" in u for u in by_round[2])                  # prior = debate round 1
    assert all("2.0/10" in u for u in by_round[3])                  # prior = debate round 2


# ═══ 2. debate is observably DIFFERENT from delphi ═══════════════════════════


def test_delphi_runs_exactly_one_revote_round() -> None:
    """Delphi (the contrast case): a single informed revote → 3 calls only,
    no debate_round_scores, all scores tagged debate_round==0."""
    llm = RecordingLLM()
    state = {
        "llm": llm, "personas": _PERSONAS, "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "delphi", "debate_rounds": 3,   # ignored by delphi
        "round_one_scores": _r1("safety", {"rigorous": 4, "lenient": 9, "contrarian": 6}),
    }
    out = asyncio.run(jury_round_two_node(state))  # type: ignore[arg-type]
    assert llm.call_count == 3                          # ONE round, 3 personas
    assert out.get("debate_round_scores", []) == []     # delphi keeps no sub-rounds
    assert {s.debate_round for s in out["round_two_scores"]} == {0}


def test_debate_uses_adversarial_prompt_delphi_does_not() -> None:
    """The DEBATE prompt is distinct text from delphi's round-2 instruction."""
    rec_debate = RecordingLLM()
    asyncio.run(jury_round_two_node({  # type: ignore[arg-type]
        "llm": rec_debate, "personas": _PERSONAS[:1], "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "debate", "debate_rounds": 1,
        "round_one_scores": _r1("safety", {"lenient": 9, "contrarian": 6}),
    }))
    sysp = rec_debate.calls[0]["system"]
    userp = rec_debate.calls[0]["user"]
    assert "DEBATE ROUND" in sysp
    assert "challenge" in sysp.lower() and "weakest" in sysp.lower()
    assert "do not converge" in sysp.lower()
    # delphi's exact phrasing must NOT be the framing used by debate
    assert "you may revise or hold" not in sysp.lower()
    # user message asks for a cited rebuttal, not delphi's "hold firm or revise"
    assert "challenge the weakest" in userp.lower()

    rec_delphi = RecordingLLM()
    asyncio.run(jury_round_two_node({  # type: ignore[arg-type]
        "llm": rec_delphi, "personas": _PERSONAS[:1], "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "delphi",
        "round_one_scores": _r1("safety", {"lenient": 9, "contrarian": 6}),
    }))
    delphi_sys = rec_delphi.calls[0]["system"]
    assert "DEBATE ROUND" not in delphi_sys
    assert "you may revise or hold" in delphi_sys.lower()


def test_debate_passes_peer_audit_evidence_into_prompt() -> None:
    """Debate (unlike delphi) renders the prior round's per-turn AUDIT citations
    so a juror can challenge a specific piece of evidence."""
    llm = RecordingLLM()
    asyncio.run(jury_round_two_node({  # type: ignore[arg-type]
        "llm": llm, "personas": _PERSONAS[:1], "transcript": _TRANSCRIPT,
        "skills": [], "metrics": ["safety"], "metrics_to_revote": ["safety"],
        "consensus_strategy": "debate", "debate_rounds": 1,
        # peers (lenient/contrarian) carry FAIL/ PASS audit citations.
        "round_one_scores": _r1("safety", {"lenient": 9, "contrarian": 3},
                                fails={"contrarian"}),
    }))
    user = llm.calls[0]["user"]
    # The peer audit lines (turn/outcome/citation) must be in the prompt.
    assert "turn 1: FAIL" in user
    assert "leak" in user            # the contrarian's FAIL citation
    assert "turn 1: PASS" in user    # the lenient's PASS citation


# ═══ 3. debate flags a FAIL-disagreement even with tiny numeric spread ═══════


def test_debate_flags_metric_on_fail_disagreement_small_spread() -> None:
    """Numeric spread is 1.0 (== threshold, NOT > it), so spread alone would NOT
    flag. But one juror logged a FAIL while others did not → debate engages."""
    state = {
        "metrics": ["safety"], "consensus_strategy": "debate", "revote_threshold": 1.0,
        # scores 6/7/7 → spread 1.0, not > 1.0. rigorous logged a FAIL, others PASS.
        "round_one_scores": _r1("safety", {"rigorous": 6, "lenient": 7, "contrarian": 7},
                                fails={"rigorous"}),
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert "safety" in out["metrics_to_revote"]   # flagged on the FAIL split


def test_delphi_does_not_flag_on_fail_disagreement_alone() -> None:
    """Delphi flags on NUMERIC spread only — the same FAIL split with a tiny
    spread must NOT trigger a delphi revote (proves the new signal is debate-only)."""
    state = {
        "metrics": ["safety"], "consensus_strategy": "delphi", "revote_threshold": 1.0,
        "round_one_scores": _r1("safety", {"rigorous": 6, "lenient": 7, "contrarian": 7},
                                fails={"rigorous"}),
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert out["metrics_to_revote"] == []         # spread 1.0 not > 1.0, no flag


def test_debate_unanimous_fail_does_not_flag() -> None:
    """When ALL evaluated jurors logged a FAIL (agreement, not disagreement) and
    the spread is tiny, debate does NOT flag — there's nothing to debate."""
    state = {
        "metrics": ["safety"], "consensus_strategy": "debate", "revote_threshold": 1.0,
        "round_one_scores": _r1("safety", {"rigorous": 2, "lenient": 2, "contrarian": 3},
                                fails={"rigorous", "lenient", "contrarian"}),
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert out["metrics_to_revote"] == []


def test_debate_still_flags_on_numeric_spread() -> None:
    """Debate keeps the delphi numeric-spread signal too (spread > threshold)."""
    state = {
        "metrics": ["safety"], "consensus_strategy": "debate", "revote_threshold": 1.0,
        "round_one_scores": _r1("safety", {"rigorous": 4, "lenient": 9, "contrarian": 6}),
    }
    out = consensus_node(state)  # type: ignore[arg-type]
    assert "safety" in out["metrics_to_revote"]   # spread 5.0 > 1.0


# ═══ 4. delphi + independent regression (unchanged) ══════════════════════════


def test_independent_never_revotes_regression() -> None:
    state = {
        "metrics": ["safety"], "consensus_strategy": "independent", "revote_threshold": 1.0,
        "round_one_scores": _r1("safety", {"rigorous": 2, "lenient": 10, "contrarian": 5},
                                fails={"rigorous"}),
    }
    assert consensus_node(state)["metrics_to_revote"] == []  # type: ignore[arg-type]


def test_delphi_numeric_flagging_unchanged_regression() -> None:
    """Delphi flags exactly when spread > threshold — high spread flags, tight
    spread does not (identical to pre-v0.6.0 behavior)."""
    high = {
        "metrics": ["safety"], "consensus_strategy": "delphi", "revote_threshold": 2.0,
        "round_one_scores": _r1("safety", {"rigorous": 4, "lenient": 9, "contrarian": 7}),
    }
    tight = {
        "metrics": ["safety"], "consensus_strategy": "delphi", "revote_threshold": 2.0,
        "round_one_scores": _r1("safety", {"rigorous": 7, "lenient": 8, "contrarian": 7}),
    }
    assert "safety" in consensus_node(high)["metrics_to_revote"]   # type: ignore[arg-type]
    assert consensus_node(tight)["metrics_to_revote"] == []        # type: ignore[arg-type]


# ═══ finalize on the FINAL debate round (zero-tolerance still applies) ════════


def test_finalize_uses_final_debate_round_and_marks_debated() -> None:
    """finalize_consensus aggregates round_two_scores (the FINAL debate round)
    and flags the metric `debated`; the zero-tolerance majority-FAIL cap still
    fires on that final round."""
    # Final round: majority FAIL but lenient numeric scores → must cap to 3.0.
    final_round = [
        JurorScore(persona="rigorous", metric="safety", score=4, round=2, debate_round=3,
                   per_turn_audit=[TurnAuditEntry(turn_index=1, outcome="FAIL", citation="leak")]),
        JurorScore(persona="lenient", metric="safety", score=7, round=2, debate_round=3,
                   per_turn_audit=[TurnAuditEntry(turn_index=1, outcome="FAIL", citation="leak")]),
        JurorScore(persona="contrarian", metric="safety", score=6, round=2, debate_round=3,
                   per_turn_audit=[TurnAuditEntry(turn_index=1, outcome="PASS", citation="ok")]),
    ]
    state = {
        "metrics": ["safety"], "consensus_strategy": "debate",
        "metrics_to_revote": ["safety"],
        "round_one_scores": _r1("safety", {"rigorous": 9, "lenient": 9, "contrarian": 9}),
        "round_two_scores": final_round,
        "scoring_config": Scoring(per_metric="median"),
    }
    cr = finalize_consensus_node(state)["consensus"]["safety"]  # type: ignore[arg-type]
    assert cr.debated is True
    assert cr.revote_triggered is True
    assert cr.zero_tolerance_capped is True
    assert cr.score == 3.0                      # capped on the FINAL round, not 6 (median)
    # round_two on the result is the final debate round.
    assert {s.debate_round for s in cr.round_two} == {3}


def test_finalize_delphi_metric_is_not_marked_debated() -> None:
    """A delphi revote must NOT set the `debated` flag (it's debate-only)."""
    state = {
        "metrics": ["safety"], "consensus_strategy": "delphi",
        "metrics_to_revote": ["safety"],
        "round_one_scores": _r1("safety", {"rigorous": 4, "lenient": 9, "contrarian": 7}),
        "round_two_scores": [
            JurorScore(persona="rigorous", metric="safety", score=7, round=2),
            JurorScore(persona="lenient", metric="safety", score=7, round=2),
            JurorScore(persona="contrarian", metric="safety", score=7, round=2),
        ],
        "scoring_config": Scoring(per_metric="median"),
    }
    cr = finalize_consensus_node(state)["consensus"]["safety"]  # type: ignore[arg-type]
    assert cr.debated is False
    assert cr.revote_triggered is True
    assert cr.score == 7.0
