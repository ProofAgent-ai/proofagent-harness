"""Re-test what broke the agent before, explore the rest (regression.py)."""
from proofagent_harness import regression as R
from proofagent_harness.agents import planner as P
from proofagent_harness.schemas import CheckVerdict, Report, Trap, Turn, TurnSpec


def _trap(name):
    return Trap(name=name, family="prompt_injection", seeds=["s"], pass_criteria="p", checks=["obeyed_injected_instruction"])


def _report(turns, fails):
    rep = Report.model_construct(transcript=turns, check_verdicts=[
        CheckVerdict(check_id="obeyed_injected_instruction", turn_index=i, observed=(i in fails), decided_by="code")
        for i in range(1, len(turns) + 1)], metadata={})
    return rep


def test_fail_then_retest_then_fixed(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOFAGENT_RETEST", "1")
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    a, b = _trap("trap_a"), _trap("trap_b")
    st = {"seed": None, "role": "r", "business_case": "b", "goal": "g", "traps": [a, b]}
    run1 = [Turn(turn_index=1, question="Q1 +markers", answer="x", trap_name="trap_a", attack_text="Q1"),
            Turn(turn_index=2, question="Q2", answer="y", trap_name="trap_b", attack_text="Q2")]
    s1 = R.update(st, _report(run1, {1}))
    assert len(s1["new"]) == 1 and not s1["retested"]

    # next run: the failed attack is re-tested word for word (pre-marker text)
    specs = R.retest_specs(st, 10)
    assert [(t.name, it["text"]) for t, it in specs] == [("trap_a", "Q1")]
    turns = [TurnSpec(turn=i + 1, trap=_trap(f"x{chr(97 + i)}")) for i in range(6)]
    turns, note = P._insert_retests(st, turns, [])
    re = [t for t in turns if t.retest_of]
    assert len(re) == 1 and re[0].fixed_question == "Q1" and note.startswith("re-test")

    # the agent now holds: fixed; a new failure elsewhere: new
    iid = re[0].retest_of
    run2 = [Turn(turn_index=1, question="Q1 +new markers", answer="ok", trap_name="trap_a", attack_text="Q1", retest_of=iid),
            Turn(turn_index=2, question="Q3", answer="z", trap_name="trap_b", attack_text="Q3")]
    s2 = R.update(st, _report(run2, {2}))
    assert [r["turn"] for r in s2["fixed"]] == [1] and [r["turn"] for r in s2["new"]] == [2]
    assert [it["text"] for it in R.open_items(R.load(R.agent_key(st)))] == ["Q3"]


def test_fixed_seed_keeps_the_reproducible_exam(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOFAGENT_RETEST", "1")
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    assert not R.enabled({"seed": 42}) and R.enabled({"seed": None})


def test_retests_take_at_most_half_the_exam(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOFAGENT_RETEST", "1")
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    st = {"seed": None, "role": "r", "business_case": "b", "goal": "g", "traps": [_trap("trap_a")]}
    led = {"items": {f"i{k}": {"trap": "trap_a", "text": f"T{k}", "status": "open", "last_failed": str(k)}
                     for k in range(20)}}
    R.save(R.agent_key(st), led)
    assert len(R.retest_specs(st, 10)) == 5


def test_a_run_that_drops_a_stale_transcript_saves_its_own(monkeypatch, tmp_path):
    """The stale stored transcript stayed forever: every run dropped it on plan drift and saved nothing."""
    from types import SimpleNamespace
    from proofagent_harness import harness as H
    saved = []
    monkeypatch.setattr("proofagent_harness.calibration.save_transcript", lambda fp, turns, **k: saved.append(fp))
    for src, expect in [("generated", True), ("fresh (stored transcript abandoned on plan drift)", True),
                        ("replayed", False)]:
        saved.clear()
        cal = SimpleNamespace(transcript_source=src, fingerprint="fp", agent_class="stable", agent_determinism=1.0)
        H._persist_transcript(cal, [Turn(turn_index=1, question="q", answer="a")])
        assert bool(saved) is expect, src


def test_short_exam_with_no_plain_round_still_retests(monkeypatch, tmp_path):
    """A 5-turn exam of control + arc steps + follow-ups left no plain slot, so nothing was re-tested (live run)."""
    monkeypatch.setenv("PROOFAGENT_RETEST", "1")
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    st = {"seed": None, "role": "r", "business_case": "b", "goal": "g", "traps": [_trap("trap_a")]}
    R.save(R.agent_key(st), {"items": {f"i{k}": {"trap": "trap_a", "text": f"T{k}", "status": "open",
                                                 "last_failed": str(k)} for k in range(3)}})
    turns = [TurnSpec(turn=1, trap=_trap("ctl"), is_control=True),
             TurnSpec(turn=2, trap=_trap("arc1"), intent_note="plant"),
             TurnSpec(turn=3, trap=_trap("arc2"), intent_note="press"),
             TurnSpec(turn=4, trap=_trap("base")),
             TurnSpec(turn=5, trap=_trap("fu"), is_follow_up=True)]
    turns, note = P._insert_retests(st, turns, [])
    re = [t.turn for t in turns if t.retest_of]
    assert len(re) == 2 and note.startswith("re-test")
    assert turns[0].is_control and not turns[0].retest_of        # the control is never replaced
    assert not turns[3].retest_of                                 # turn 4 keeps its follow-up's anchor
