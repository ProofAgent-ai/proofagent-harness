"""The plan keeps the turn count asked for, however heavily it is woven (tracker batch 2026-11-11: 30 requested,
10 run on one agent, 31-34 on six others)."""
import random

from proofagent_harness.agents import planner as P
from proofagent_harness.loaders import load_traps
from proofagent_harness.schemas import TurnSpec

M = ["safety", "task_success", "tool_use", "hallucination_resistance", "instruction_following",
     "manipulation_resistance"]


def _plan(lib, n, s):
    r = random.Random(s)
    turns = [TurnSpec(turn=i + 1, trap=t, target_behavior=t.pass_criteria) for i, t in enumerate(r.sample(lib, n))]
    for t in turns:
        if t.turn > 2:
            x = r.random()
            if x < 0.45:
                t.is_follow_up = True
            elif x < 0.9:
                t.callback_to_turn = r.randint(1, t.turn - 1)
    turns = P._inherit_traps_for_follow_ups(turns)
    turns = P._attack_arc(turns, lib, ["search_units"], pinned=[], seed=42)
    turns = P._insert_control_turns(turns, pinned=[])
    turns, _ = P._enforce_mix_on_turns(turns, lib, [], M, ["hr"], has_tools=True, seed=42)
    turns, _ = P._ensure_domain_share(turns, lib, [], M, ["hr"], seed=42)
    turns, _ = P._vary_final(turns, lib, [], M, ["hr"], seed=42)
    turns, _ = P._pressure_thread(turns)
    return turns


def test_woven_plans_keep_their_length():
    lib = load_traps()
    for n in (8, 20, 30):
        for s in range(40):
            assert len(_plan(lib, n, s)) == n, (n, s)


def test_same_seed_same_plan():
    lib = load_traps()
    a = [t.trap.name for t in _plan(lib, 30, 7)]
    b = [t.trap.name for t in _plan(lib, 30, 7)]
    assert a == b
