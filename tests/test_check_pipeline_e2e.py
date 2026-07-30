"""Full graph, check scoring active, all the way to a Report.

The crash that reached a user got past every node except the last one, because no test
ran the whole pipeline with check scoring on. This does, so the tail of the run —
compliance assessor, reporter, `_state_to_report` — is exercised without paying for a
real evaluation.
"""

from __future__ import annotations

import re
from typing import Any

from proofagent_harness import Harness
from proofagent_harness.schemas import Event

from .conftest import FakeLLM


class VotingLLM(FakeLLM):
    """A stub that speaks the CHECK BALLOT schema, the way a real juror now must.

    Answers whatever questions the prompt actually contains, so the ballot lines up with
    what the run asked rather than with a hardcoded guess.
    """

    model: str = "fake/voting"

    async def complete_json(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        self.last_temperature_per_call.append(temperature)
        self.call_count += 1
        self.total_tokens += 15

        props = ((schema or {}).get("properties") or {})
        if "check_votes" in props:
            text = "\n".join(m.get("content", "") for m in messages)
            votes = []
            turn = 0
            for line in text.splitlines():
                m_turn = re.match(r"^### Turn (\d+)", line.strip())
                if m_turn:
                    turn = int(m_turn.group(1))
                    continue
                m_check = re.match(r"^- `([a-z_]+)`", line.strip())
                if m_check and turn:
                    cid = m_check.group(1)
                    # Fail one check deliberately so the compliance join has something
                    # to report; everything else passes with a quote.
                    observed = cid == "disclosed_to_unentitled_requester"
                    votes.append({
                        "check_id": cid, "turn_index": turn,
                        "observed": observed,
                        "quote": "stub verbatim quote" if observed else "",
                    })
            return {"check_votes": votes, "reasoning": "stub ballot"}

        # Any non-ballot call (planner, reporter, follow-up weaving) gets a shape that
        # satisfies the widest set of callers.
        return {
            "score": 7.5,
            "reasoning": "deterministic stub",
            "per_turn_audit": [
                {"turn_index": 1, "outcome": "PASS", "citation": "stub citation"}
            ],
            "turns": [],
            "findings": [],
            "frameworks": [],
        }


def _agent(question: str) -> str:
    return "I can't share that. I've escalated this to a supervisor instead."


def test_full_pipeline_completes_with_check_scoring_and_derived_compliance():
    events: list[Event] = []
    llm = VotingLLM()
    report = Harness(
        llm=llm, turns=3, seed=42, verbose=False,
        personas=["rigorous"],
    ).evaluate(
        _agent,
        role="a credit support agent",
        business_case="consumer lending",
        goal="answer borrower questions without leaking data",
        assess_compliance=True,
        on_event=events.append,
    )

    # The run finished and produced a Report rather than raising in a late node.
    assert report is not None
    assert report.per_metric, "no metrics survived the check path"

    # Every event constructed during a real run validated against the schema.
    assert all(isinstance(e, Event) for e in events)

    details = " | ".join(e.detail for e in events)
    # The check path actually engaged — not a silent fallback to holistic scoring.
    assert "code layer settled" in details
    assert "scored from" in details and "check verdict" in details
    # Round 2 must be the blind resample, never the peer-visible revote that herded.
    assert "round 2 (informed)" not in details

    # Compliance came from the join, with no assessor call. Asserted unconditionally:
    # guarding this behind `if report.compliance` would let the whole derivation
    # silently disappear and still pass.
    comp = report.compliance or {}
    assert comp, "compliance was requested but nothing was produced"
    assert comp.get("derivation") == "checks"
    assert comp.get("model") == "derived (check verdicts)"
    assert any(
        c["status"] != "not_evaluated"
        for fw in comp["frameworks"] for c in fw["controls"]
    ), "the join produced no assessed control"


def test_code_verdicts_survive_the_graph_and_reach_the_score():
    """`code_verdicts` is a declared channel; an undeclared one is dropped silently.

    If it were lost, every metric would score purely on juror opinion while the run
    still reported that the code layer had settled verdicts.
    """
    import proofagent_harness.agents.consensus as consensus_mod

    seen: list[tuple[int, int]] = []
    original = consensus_mod.pool_check_votes

    def spy(state):
        pooled = original(state)
        seen.append((
            len(state.get("code_verdicts") or []),
            sum(1 for v in pooled if v.decided_by == "code"),
        ))
        return pooled

    consensus_mod.pool_check_votes = spy
    try:
        Harness(
            llm=VotingLLM(), turns=3, seed=42, verbose=False, personas=["rigorous"],
        ).evaluate(_agent, role="r", business_case="b", goal="g")
    finally:
        consensus_mod.pool_check_votes = original

    assert seen, "consensus never pooled votes"
    in_state, in_pool = seen[-1]
    assert in_state > 0, "code_verdicts was dropped between the jury and consensus"
    assert in_pool > 0, "code verdicts never reached the pooled scoring set"


def test_check_scoring_can_be_switched_off_for_an_ab_comparison(monkeypatch):
    """The A/B escape hatch must still reach a Report on the older path."""
    monkeypatch.setenv("PROOFAGENT_CHECK_SCORING", "0")
    report = Harness(
        llm=FakeLLM(), turns=3, seed=42, verbose=False, personas=["rigorous"],
    ).evaluate(_agent, role="r", business_case="b", goal="g")
    assert report.per_metric
