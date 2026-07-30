"""Calibration phase — fingerprinting, transcript reuse, and the pass-count ladder.

Locks the properties the scoring policy depends on:
  * the fingerprint moves when anything under test moves, and only then;
  * a stored transcript is reused, and never spliced into the wrong position;
  * the pass count is chosen from a MEASURED repeat spread, not assumed;
  * every failure path degrades to a plain single-pass run.
"""

from __future__ import annotations

import asyncio
import json

from proofagent_harness import calibration as cal
from proofagent_harness.schemas import Turn


def _run(coro):
    return asyncio.run(coro)


# ── fingerprint ──────────────────────────────────────────────────────────────

def test_fingerprint_is_order_independent_but_input_sensitive() -> None:
    a = cal.fingerprint(turns=8, metrics=["safety", "tool_use"], personas=["x", "y"])
    b = cal.fingerprint(turns=8, metrics=["tool_use", "safety"], personas=["y", "x"])
    assert a == b                                      # set-like inputs are sorted
    assert a != cal.fingerprint(turns=9, metrics=["safety", "tool_use"])
    assert a != cal.fingerprint(turns=8, metrics=["safety"])


def test_fingerprint_tracks_every_axis_of_the_run() -> None:
    base = {"turns": 4, "metrics": ["safety"], "consensus": "delphi", "llm": "m1"}
    fp = cal.fingerprint(**base)
    assert fp != cal.fingerprint(**{**base, "consensus": "debate"})
    assert fp != cal.fingerprint(**{**base, "llm": "m2"})
    assert fp != cal.fingerprint(**{**base, "fallback_llm": "m3"})
    assert fp != cal.fingerprint(**{**base, "traps": ["t1"]})


def test_fingerprint_follows_agent_file_contents(tmp_path) -> None:
    f = tmp_path / "agent.py"
    f.write_text("def agent(m): return 'v1'", encoding="utf-8")
    before = cal.fingerprint(agent_source=f, turns=2)
    f.write_text("def agent(m): return 'v2'", encoding="utf-8")
    assert cal.fingerprint(agent_source=f, turns=2) != before


def test_fingerprint_survives_a_missing_path() -> None:
    assert cal.fingerprint(agent_source="/nope/agent.py", turns=2)


# ── transcript reuse ─────────────────────────────────────────────────────────

def test_transcript_round_trips_through_the_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    turns = [Turn(turn_index=0, trap_name="t", question="q", answer="a")]
    cal.save_transcript("fp1", turns, agent={"agent_class": cal.VOLATILE})
    got = cal.load_transcript("fp1")
    assert got is not None
    stored, measured = got
    assert stored[0]["answer"] == "a"
    # The agent measurement rides along so a replay keeps the same scoring policy.
    assert measured["agent_class"] == cal.VOLATILE


def test_replay_inherits_the_generating_runs_agent_class(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path / "home"))
    report = tmp_path / "run.json"
    report.write_text(json.dumps({
        "metadata": {"fingerprint": "fp9", "agent_class": cal.VOLATILE,
                     "agent_determinism": 0.4},
        "transcript": [{"turn_index": 0, "trap_name": "t", "question": "q", "answer": "a"}],
    }), encoding="utf-8")
    got = cal.load_transcript("fp9", search=[tmp_path])
    assert got is not None
    _turns, measured = got
    assert measured["agent_class"] == cal.VOLATILE
    assert measured["agent_determinism"] == 0.4


def test_transcript_is_read_from_a_report_carrying_the_fingerprint(tmp_path, monkeypatch) -> None:
    # This is the cross-machine path: a colleague has the shared report, not the cache.
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path / "home"))
    report = tmp_path / "run.json"
    report.write_text(json.dumps({
        "metadata": {"fingerprint": "fp2"},
        "transcript": [{"turn_index": 0, "trap_name": "t", "question": "q", "answer": "a"}],
    }), encoding="utf-8")
    assert cal.load_transcript("fp2", search=[tmp_path]) is not None
    assert cal.load_transcript("other", search=[tmp_path]) is None


def test_missing_or_corrupt_store_returns_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    assert cal.load_transcript("absent") is None
    (tmp_path / "transcripts").mkdir(parents=True)
    (tmp_path / "transcripts" / "bad.json").write_text("{not json", encoding="utf-8")
    assert cal.load_transcript("bad") is None


def test_replay_never_splices_the_wrong_turn() -> None:
    from proofagent_harness.agents.conductor import _replay_turn

    c = cal.Calibration(
        transcript_source="replayed",
        replay=[{"turn_index": 0, "trap_name": "expected", "question": "q", "answer": "a"}],
    )
    state = {"calibration": c}
    assert _replay_turn(state, 0, "expected") is not None
    assert _replay_turn(state, 5, "expected") is None    # past the end
    assert _replay_turn({"calibration": None}, 0, "expected") is None


def test_a_drifted_plan_abandons_the_stored_transcript() -> None:
    """A drifted turn is REFUSED, and the rest of the reuse goes with it.

    This reverses the earlier rule. Trusting the stored turn was right when a juror
    formed a holistic judgment about an answer: the transcript was what got scored, and
    falling through to a live call for one turn produced a run that claimed `replayed`
    while regenerating 1 of 15 turns.

    Once traps carry `checks:`, the trap at position N supplies the questions that
    answer is scored against — so a mismatch scores one trap's checks against another
    trap's answer. Observed: drift at 2 of 8 turns produced a flat 100% on all six
    metrics and 100% compliance. Partial reuse is still refused for the original reason,
    which is why the whole transcript is dropped rather than just the drifted turn.
    """
    from proofagent_harness.agents.conductor import _replay_turn

    c = cal.Calibration(
        transcript_source="replayed",
        replay=[
            {"turn_index": 0, "trap_name": "stored", "question": "q", "answer": "a"},
            {"turn_index": 1, "trap_name": "also_stored", "question": "q2", "answer": "b"},
        ],
    )
    events: list = []
    state = {"calibration": c, "on_event": events.append}

    assert _replay_turn(state, 0, "planned") is None, "a mismatched turn must not replay"
    assert any("plan drift" in str(e.detail) for e in events)
    # Reuse is abandoned wholesale, so a later turn that WOULD have matched also runs
    # live — the run is honestly fresh rather than half replayed.
    assert not c.replay
    assert _replay_turn(state, 1, "also_stored") is None
    assert "fresh" in c.transcript_source


def test_generated_source_does_not_replay() -> None:
    c = cal.Calibration(transcript_source="generated", replay=[{"trap_name": "t"}])
    assert c.replaying is False
    assert c.turn_at(0) is None


# ── jury pass ladder ─────────────────────────────────────────────────────────

def test_a_stable_scorer_stays_single_pass(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))

    async def steady():
        return {"safety": 8.0, "tool_use": 9.0}

    residual, k = _run(cal.measure_jury(steady, key="steady"))
    assert residual == 0.0
    assert k == 1


def test_damping_is_off_by_default_so_an_unstable_scorer_stays_single_pass(
    tmp_path, monkeypatch,
) -> None:
    """Measured: K=5 gave a 28.2 pp spread where K=1 gave 26.6 — repeat passes damp the
    SCORER, and what survives is the AGENT moving between runs. So the ladder is off by
    default; the residual is still measured and reported."""
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("PROOFAGENT_JURY_DAMPING", raising=False)
    seq = iter([8.0, 2.0] * 40)

    async def jumpy():
        return {"safety": next(seq)}

    residual, k = _run(cal.measure_jury(jumpy, key="nodamp"))
    assert k == 1                       # no escalation
    assert residual > cal.TOLERANCE     # but the instability IS reported


def test_an_unstable_scorer_escalates_the_pass_count(tmp_path, monkeypatch) -> None:
    # The returned residual is the spread AT the chosen pass count — i.e. the
    # guarantee the run ships with — so escalation shows up as k > 1, not as a
    # large residual. A single pass here would disagree by 6.0.
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PROOFAGENT_JURY_DAMPING", "1")
    seq = iter([8.0, 2.0] * 40)

    async def jumpy():
        return {"safety": next(seq)}

    residual, k = _run(cal.measure_jury(jumpy, key="jumpy"))
    assert k > 1
    assert residual <= cal.TOLERANCE


def test_the_ladder_gives_up_rather_than_looping(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PROOFAGENT_JURY_DAMPING", "1")
    seq = iter(range(1000))

    async def chaos():
        return {"safety": float(next(seq))}

    residual, k = _run(cal.measure_jury(chaos, key="chaos"))
    assert k == cal._LADDER_ON[-1]      # capped, never unbounded
    assert residual > cal.TOLERANCE     # and honest that it did not converge


def test_a_measured_profile_is_cached_and_reused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    calls = {"n": 0}

    async def counted():
        calls["n"] += 1
        return {"safety": 8.0}

    _run(cal.measure_jury(counted, key="shared"))
    first = calls["n"]
    _run(cal.measure_jury(counted, key="shared"))
    assert calls["n"] == first          # second run measured nothing


def test_an_unmeasurable_scorer_is_unmeasured_not_stable(tmp_path, monkeypatch) -> None:
    """A failed measurement must never read as "perfectly stable".

    The earlier version returned 0.0, which cached a permanent "stable, one pass"
    verdict for a scorer that had never actually been observed."""
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))

    async def empty():
        return {}

    residual, k = _run(cal.measure_jury(empty, key="empty"))
    assert residual is None          # unmeasured, NOT 0.0
    assert k == 1
    assert not (tmp_path / "profiles" / "empty.json").exists()   # and never cached


def test_an_unmeasured_residual_survives_into_metadata_as_null() -> None:
    m = cal.Calibration(jury_residual=None).to_metadata()
    assert m["jury_residual"] is None


# ── agent replay behaviour ───────────────────────────────────────────────────

def test_identical_replies_need_no_judgment() -> None:
    judged = {"n": 0}

    async def ask(_):
        return "exactly the same"

    async def judge(_p, _r):
        judged["n"] += 1
        return {"equivalent": True}

    cls, det, drift = _run(cal.measure_agent(ask, ["p1", "p2"], judge=judge))
    assert (cls, det, drift) == (cal.DETERMINISTIC, 1.0, [])
    assert judged["n"] == 0            # a proof, so nothing to judge


def test_wording_drift_is_stable_not_volatile() -> None:
    n = iter(range(100))

    async def ask(_):
        return f"approved, tier 2 ({next(n)})"

    async def judge(_p, _r):
        return {"equivalent": True}

    cls, det, _ = _run(cal.measure_agent(ask, ["p1"], judge=judge))
    assert cls == cal.STABLE
    assert det < 1.0


def test_behavioural_drift_is_volatile_and_names_the_dimension() -> None:
    n = iter(range(100))

    async def ask(_):
        return f"reply {next(n)}"

    async def judge(_p, _r):
        return {"equivalent": False, "drifted": ["outcome"]}

    cls, _det, drift = _run(cal.measure_agent(ask, ["p1"], judge=judge))
    assert cls == cal.VOLATILE
    assert drift == ["outcome"]


def test_no_judge_available_falls_back_to_stable() -> None:
    n = iter(range(100))

    async def ask(_):
        return f"reply {next(n)}"

    cls, _d, _dr = _run(cal.measure_agent(ask, ["p1"], judge=None))
    assert cls == cal.STABLE


def test_the_probe_budget_stays_small() -> None:
    """The volatility question is binary, so the probe is deliberately cheap: 4-6 agent
    calls, not the 9 an earlier 3x3 configuration spent to reach the same verdict."""
    assert cal.probe_count(8) * cal._REPLIES <= 6
    assert cal.probe_count(100) * cal._REPLIES <= 6
    assert cal.probe_count(1) >= cal._MIN_PROBES


# ── policy plumbing ──────────────────────────────────────────────────────────

def test_metadata_carries_every_field_that_was_measured() -> None:
    c = cal.Calibration(
        fingerprint="fp", transcript_source="replayed", agent_class=cal.VOLATILE,
        agent_determinism=0.5, jury_residual=0.25, k_metrics=3, k_compliance=3,
    )
    m = c.to_metadata()
    assert m["fingerprint"] == "fp"
    assert m["transcript_source"] == "replayed"
    assert m["scoring_passes"] == 3
    assert m["compliance_passes"] == 3
    assert m["agent_determinism"] == 0.5


def test_jury_pass_count_reaches_the_scorer() -> None:
    from proofagent_harness.agents.juror import _scoring_passes

    assert _scoring_passes({"calibration": None}) == 1
    assert _scoring_passes({"calibration": cal.Calibration(k_metrics=3)}) == 3
    assert _scoring_passes({"calibration": cal.Calibration(k_metrics=999)}) <= 9


def test_compliance_pass_count_prefers_an_explicit_override(monkeypatch) -> None:
    from proofagent_harness.agents.compliance_assessor import _passes

    state = {"calibration": cal.Calibration(k_compliance=3)}
    assert _passes(state) == 3
    monkeypatch.setenv("PROOFAGENT_COMPLIANCE_PASSES", "5")
    assert _passes(state) == 5          # the env var still wins
    assert _passes(None) == 5


def test_calibration_can_be_switched_off(monkeypatch) -> None:
    assert cal.enabled() is True
    monkeypatch.setenv("PROOFAGENT_CALIBRATION", "0")
    assert cal.enabled() is False


# ── the three consistency fixes ───────────────────────────────────────────────

def test_governance_scope_keys_on_assessed_controls_not_list_length() -> None:
    """Five frameworks with nothing assessed is the same evidence as none at all.

    Keying scope on the frameworks LIST made two runs of an identical transcript
    score 10 governance points apart, because the assessor returned 0 frameworks
    once and 5 (all empty) the next."""
    from proofagent_harness.scoring.pai import pai_from_report

    base = {"final_score": 8.0, "per_metric": {"safety": 8.0},
            "context_engineering": {"score": 8.0}, "findings": [], "technical_issues": []}
    none_at_all = pai_from_report({**base, "compliance": {"frameworks": []}})
    five_empty = pai_from_report({**base, "compliance": {"frameworks": [
        {"id": f"f{i}", "controls": [{"status": "not_evaluated"}] * 6} for i in range(5)
    ]}})
    g1 = next(a.score for a in none_at_all.axes if a.key == "governance")
    g2 = next(a.score for a in five_empty.axes if a.key == "governance")
    assert g1 == g2
    assert none_at_all.score == five_empty.score


def test_assessed_controls_do_earn_scope_credit() -> None:
    from proofagent_harness.scoring.pai import pai_from_report

    base = {"final_score": 8.0, "per_metric": {"safety": 8.0},
            "context_engineering": {"score": 8.0}, "findings": [], "technical_issues": []}
    empty = pai_from_report({**base, "compliance": {"frameworks": [
        {"id": "f", "controls": [{"status": "not_evaluated"}] * 6}]}})
    clean = pai_from_report({**base, "compliance": {"frameworks": [
        {"id": "f", "controls": [{"status": "met"}] * 6}]}})
    g_empty = next(a.score for a in empty.axes if a.key == "governance")
    g_clean = next(a.score for a in clean.axes if a.key == "governance")
    assert g_clean > g_empty


def test_compliance_spread_is_measured_from_the_passes_themselves() -> None:
    from proofagent_harness.agents.compliance_assessor import _spread

    def fw(met: int, attention: int) -> dict:
        return {"frameworks": [{"id": "f", "controls":
                [{"status": "met"}] * met + [{"status": "attention"}] * attention}]}

    assert _spread([fw(6, 0), fw(6, 0)]) == 0.0            # agreeing passes
    assert _spread([fw(6, 0), fw(0, 6)]) == 100.0          # 100 vs 0
    assert _spread([fw(6, 0)]) is None                     # one pass -> unmeasured
    assert _spread([]) is None
    assert _spread([{"frameworks": []}, {"frameworks": []}]) is None  # no evidence


def test_compliance_passes_floor_makes_the_axis_measurable() -> None:
    from proofagent_harness.agents.compliance_assessor import _passes

    # A single pass cannot be measured, so calibration never yields fewer than 3.
    assert _passes({"calibration": cal.Calibration(k_compliance=1)}) >= 3
    assert _passes({"calibration": cal.Calibration(k_compliance=5)}) == 5


def test_compliance_residual_is_a_declared_state_channel() -> None:
    # An undeclared key is dropped by LangGraph before the report is built.
    from proofagent_harness.graph.state import HarnessState

    assert "compliance_residual" in HarnessState.__annotations__


def test_a_replay_adopts_the_stored_plan_instead_of_re_deriving_it() -> None:
    """Re-planning before a replay drifted, because follow-up weaving is an LLM call.

    Measured on two runs of one command: turns 1-11 matched, turn 12 did not, so the run
    replayed 11 turns and generated 4 while reporting one `transcript_source`. The two
    reports then differed by 17.2 pp on hallucination_resistance and 10.2 pp on
    manipulation_resistance despite being the same evaluation.
    """
    from proofagent_harness.agents.planner import _plan_from_stored
    from proofagent_harness.schemas import Trap

    traps = [Trap(name="a", family="factuality"), Trap(name="b", family="bias")]
    c = cal.Calibration(
        transcript_source="replayed",
        replay=[
            {"turn_index": 0, "trap_name": "b", "question": "q", "answer": "x"},
            {"turn_index": 1, "trap_name": "a", "question": "q", "answer": "y"},
        ],
    )
    plan = _plan_from_stored({"calibration": c, "traps": traps})
    assert plan is not None
    # The STORED order wins, not whatever ranking the planner would have produced.
    assert [t.trap.name for t in plan] == ["b", "a"]
    assert [t.turn for t in plan] == [1, 2]


def test_an_unresolvable_stored_trap_falls_back_to_planning_fresh() -> None:
    """Better a fully fresh run than a partly-replayed one reported as either."""
    from proofagent_harness.agents.planner import _plan_from_stored
    from proofagent_harness.schemas import Trap

    c = cal.Calibration(
        transcript_source="replayed",
        replay=[{"turn_index": 0, "trap_name": "gone_from_the_library"}],
    )
    assert _plan_from_stored({"calibration": c, "traps": [Trap(name="a", family="bias")]}) is None


def test_no_stored_transcript_means_normal_planning() -> None:
    from proofagent_harness.agents.planner import _plan_from_stored

    assert _plan_from_stored({}) is None
    assert _plan_from_stored({"calibration": cal.Calibration()}) is None



def test_the_context_grade_is_cached_with_the_transcript(tmp_path, monkeypatch) -> None:
    """Grading the context is a non-deterministic LLM call on a FIXED artifact.

    Measured across two scorings of one transcript: `grounding_sufficiency` read 70% then
    50%, and every finding was reworded. Because the grade now weights the behavioural
    score and its findings are rendered into every juror prompt, that wobble moved
    `hallucination_resistance` 16.1 pp on an IDENTICAL transcript. So it travels with the
    transcript, exactly as the agent measurement does.
    """
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    turns = [Turn(turn_index=0, trap_name="t", question="q", answer="a")]
    grade = {"score": 5.7, "sub_criteria": [{"id": "grounding_sufficiency", "score": 5.0}]}
    cal.save_transcript("fpq", turns, agent={"agent_class": cal.STABLE}, context=grade)

    got = cal.load_transcript("fpq")
    assert got is not None
    _stored, measured = got
    assert measured["context_engineering"] == grade


def test_a_shared_report_carries_its_context_grade_too(tmp_path, monkeypatch) -> None:
    """The cross-machine door: a colleague has the report, not the cache."""
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path / "home"))
    report = tmp_path / "run.json"
    report.write_text(json.dumps({
        "metadata": {"fingerprint": "fpr"},
        "transcript": [{"turn_index": 0, "trap_name": "t", "question": "q", "answer": "a"}],
        "context_engineering": {"score": 5.7},
    }), encoding="utf-8")
    got = cal.load_transcript("fpr", search=[tmp_path])
    assert got is not None
    assert got[1]["context_engineering"] == {"score": 5.7}


def test_the_stored_grade_is_only_reused_on_an_actual_replay() -> None:
    """Reuse is gated on REPLAYING, not on a grade merely existing.

    A leftover grade from an earlier run would otherwise suppress the assessment on a
    fresh run, scoring new turns against an old context.
    """
    from proofagent_harness.agents.context_assessor import context_assessor_node

    grade = {"score": 5.7, "sub_criteria": [{"id": "injection_hardening", "score": 3.0}]}

    replaying = cal.Calibration(
        transcript_source="replayed",
        replay=[{"turn_index": 0, "trap_name": "t"}],
        context_engineering=grade,
    )
    out = context_assessor_node({"assess_context": True, "calibration": replaying})
    assert out["context_engineering"] == grade
    assert out["q_weights"]["instruction_override"] == 1.7

    # Same grade on the object, but nothing is being replayed -> must NOT be reused.
    fresh = cal.Calibration(transcript_source="generated", context_engineering=grade)
    import proofagent_harness.context_engineering as ce_mod
    called: list[int] = []
    original = ce_mod.assess_context_engineering
    ce_mod.assess_context_engineering = lambda **kw: (called.append(1), {})[1]
    try:
        context_assessor_node({"assess_context": True, "calibration": fresh})
    finally:
        ce_mod.assess_context_engineering = original
    assert called, "a fresh run must re-grade the context"



def test_fresh_skips_reuse_but_keeps_the_fingerprint(tmp_path, monkeypatch) -> None:
    """`--fresh` must defeat BOTH reuse doors, and still identify the run.

    Reuse reads the local store AND any report JSON in the working directory with a
    matching fingerprint. Clearing ~/.proofagent/transcripts therefore does not force a
    fresh run — a stale report sitting in the cwd is enough, and the only signal is
    `transcript_source: replayed` in the metadata.
    """
    from proofagent_harness import Harness

    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    turns = [Turn(turn_index=0, trap_name="t", question="q", answer="a")]

    h = Harness(llm=None, turns=1, seed=1, verbose=False, fresh=True)
    state = {
        "agent_callable": lambda q: "ok", "context": None, "traps": [], "pin_traps": [],
        "turn_count": 1, "metrics": ["safety"], "personas": [], "seed": 1,
        "role": "r", "business_case": "b", "goal": "g",
    }
    fp = h._fingerprint(state)
    cal.save_transcript(fp, turns)
    assert cal.load_transcript(fp) is not None, "stored, so reuse WOULD be possible"

    got = _run(h._calibrate(dict(state)))
    assert got is not None
    assert got.fingerprint == fp, "fresh must still identify the run"
    assert got.transcript_source == "generated"
    assert not got.replay
    assert any("fresh" in n for n in got.notes)


def test_without_fresh_the_stored_transcript_is_reused(tmp_path, monkeypatch) -> None:
    from proofagent_harness import Harness

    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    h = Harness(llm=None, turns=1, seed=1, verbose=False)
    state = {
        "agent_callable": lambda q: "ok", "context": None, "traps": [], "pin_traps": [],
        "turn_count": 1, "metrics": ["safety"], "personas": [], "seed": 1,
        "role": "r", "business_case": "b", "goal": "g",
    }
    cal.save_transcript(
        h._fingerprint(state),
        [Turn(turn_index=0, trap_name="t", question="q", answer="a")],
    )
    got = _run(h._calibrate(dict(state)))
    assert got is not None and got.transcript_source == "replayed"

