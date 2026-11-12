"""Domain fit, a 20% heavy-PII share, and a pressure thread (lessons from production 0.13.0)."""
from proofagent_harness.agents import planner as P
from proofagent_harness.schemas import Trap, TurnSpec


def _t(name, family="prompt_injection", domains=None, tags=None, universal=False):
    return Trap(name=name, family=family, seeds=["s"], pass_criteria="p",
                domains=domains or [], tags=tags or [], universal=universal)


def test_pii_share_is_twenty_percent():
    assert P.PII_SHARE == 0.20 and P.REALWORLD_SHARE == 0.20


def test_health_pii_preferred_for_healthcare_agent():
    c = [_t("pii_card_bank_indirect_pt", "data_exfiltration"), _t("pii_health_secret_direct_en", "data_exfiltration")]
    assert [t.name for t in P._domain_fit_pii(c, ["healthcare"])] == ["pii_health_secret_direct_en"]
    assert len(P._domain_fit_pii(c, ["unknown"])) == 2  # no words for the domain: all kept


def test_domain_share_swaps_off_domain_plain_rounds():
    off = [_t(f"off_{chr(97 + i)}x", "code_safety", domains=["devops"]) for i in range(10)]
    turns = [TurnSpec(turn=i + 1, trap=t) for i, t in enumerate(off)]
    lib = off + [_t(f"med_{chr(97 + i)}x", ["bias", "factuality", "social_engineering", "compliance"][i % 4],
                     domains=["healthcare"]) for i in range(8)]
    out, note = P._ensure_domain_share(turns, lib, [], ["safety"], ["healthcare"])
    fit = sum(1 for t in out if P._domain_fit(t.trap, ["healthcare"]))
    assert fit >= 5 and note
    assert len({t.trap.family for t in out}) >= 3  # fitting the domain keeps the mix varied


def test_pressure_thread_plants_then_presses_twice():
    traps = [_t(f"a{i}", "factuality" if i == 2 else "prompt_injection") for i in range(15)]
    turns = [TurnSpec(turn=i + 1, trap=t) for i, t in enumerate(traps)]
    out, note = P._pressure_thread(turns)
    pressed = [t for t in out if t.callback_to_turn == 3]
    assert len(pressed) == 2 and all("[pressure" in t.intent_note for t in pressed)
    assert all(t.turn > 4 for t in pressed) and note


def test_fit_and_vary_raises_fit_and_families():
    fams = ["code_safety", "code_safety", "code_safety", "prompt_injection", "prompt_injection",
            "prompt_injection", "prompt_injection", "verbal_abuse", "verbal_abuse", "business_logic"]
    sel = [_t(f"off{chr(97 + i)}x", f, domains=["devops"]) for i, f in enumerate(fams)]
    lib = sel + [_t(f"med{chr(97 + i)}x", f, domains=["healthcare"])
                 for i, f in enumerate(["bias", "factuality", "social_engineering", "compliance", "policy_drift", "bias"])]
    out, note = P._fit_and_vary(sel, lib, [], ["safety"], ["healthcare"])
    assert note
    assert sum(1 for t in out if P._domain_fit(t, ["healthcare"])) >= 4
    assert len({t.family for t in out}) > len(set(fams))


def test_probe_resends_the_runs_own_attacks_pii_first():
    """The probe ran on a pre-run snapshot and sent random library seeds (XML, AES, housing) into a
    medical exam; it then crashed on a stale name. It must re-send this run's own attacks."""
    import asyncio
    from proofagent_harness import calibration as C
    from proofagent_harness import harness as H
    from proofagent_harness.schemas import EvaluationPlan, Turn
    a = _t("plain_attack_x")
    b = _t("pii_health_secret_direct_en", "data_exfiltration")
    plan = EvaluationPlan(turns=[TurnSpec(turn=1, trap=a), TurnSpec(turn=2, trap=b)], active_metrics=[])
    tr = [Turn(turn_index=1, question="attack one", answer="x"), Turn(turn_index=2, question="pii attack", answer="y")]
    st = {"traps": [a, b], "plan": plan, "transcript": tr, "agent_callable": lambda m: "ok", "seed": 1, "turn_count": 5}
    assert [q for q, _ in H._probe_candidates(st)] == ["pii attack", "attack one"]
    h = H.Harness.__new__(H.Harness)
    h.turns, h.llm, h._pre_graph_warnings = 5, None, []
    asyncio.run(h._calibrate_agent(st, C))
    assert {r.get("prompt") for r in h._probe_replies} <= {"pii attack", "attack one"}


def test_real_library_medical_plan_matches_production_variety():
    """On the real library a healthcare plan keeps 10 families (production 0.13.0: 10), the PII
    share, the diversity floors and the pressure thread, with the identity attack used twice."""
    from proofagent_harness.loaders import load_traps
    lib = load_traps()
    D = ["healthcare", "privacy", "security"]
    M = ["safety", "tool_use", "hallucination_resistance", "manipulation_resistance",
         "instruction_following", "task_success"]
    sel = P._select_traps(lib, M, D, 21, seed=42)
    sel, _ = P._ensure_realworld_share(sel, lib, [], M, D, seed=42)
    sel, _ = P._ensure_pii_share(sel, lib, [], M, D, seed=42)
    sel, _ = P._ensure_tool_misuse(sel, lib, [], M, D, has_tools=True, seed=42)
    sel, _ = P._balance_mix(sel, lib, [], M, D, seed=42)
    sel, _ = P._fit_and_vary(sel, lib, [], M, D, seed=42)
    turns = [TurnSpec(turn=i + 1, trap=t) for i, t in enumerate(sel)]
    turns = P._attack_arc(turns, lib, ["verify_patient_identity"], pinned=[], seed=42)
    turns = P._insert_control_turns(turns, pinned=[])
    turns, _ = P._enforce_mix_on_turns(turns, lib, [], M, D, has_tools=True, seed=42)
    turns, _ = P._ensure_domain_share(turns, lib, [], M, D, seed=42)
    turns, _ = P._vary_final(turns, lib, [], M, D, seed=42)
    turns, note = P._pressure_thread(turns)
    a = [t for t in turns if not t.is_control]
    assert len({t.trap.family for t in a}) >= 10
    assert sum(P.is_pii_trap(t.trap) for t in a) >= 0.2 * len(a)
    assert sum(t.trap.name == "identity_plant_and_recall" for t in a) <= 2
    assert note.startswith("pressure thread")


def test_generic_domain_tag_does_not_pull_in_another_vertical():
    """'support' pulled an airline crew-impersonation trap into a bank agent's exam."""
    airline = _t("crew_x", "social_engineering", domains=["airline", "travel", "support"])
    generic = _t("generic_x", "social_engineering", domains=["support", "b2c"])
    bank = _t("bank_x", "social_engineering", domains=["finance"])
    D = {"finance", "payments", "support"}
    assert not P._domain_overlap_specific(airline, D)
    assert P._domain_overlap_specific(generic, D) and P._domain_overlap_specific(bank, D)


def test_custom_traps_are_reused_under_a_seed(tmp_path, monkeypatch):
    """Two runs, same code/models/seed, shared only 21 of 30 traps: the custom ones were regenerated."""
    import asyncio
    monkeypatch.setenv("PROOFAGENT_HOME", str(tmp_path))
    calls = []

    async def fake(state, n):
        calls.append(n)
        return [_t(f"custom_x{chr(97 + i)}", "data_exfiltration") for i in range(n)]

    monkeypatch.setattr(P, "_generate_custom_traps_llm", fake)
    st = {"seed": 42, "role": "r", "business_case": "b", "goal": "g", "metrics": ["safety"], "traps": []}
    a = asyncio.run(P._generate_custom_traps(st, 3))
    b = asyncio.run(P._generate_custom_traps(st, 3))
    assert [t.name for t in a] == [t.name for t in b] and calls == [3]
    st2 = dict(st, seed=None)
    asyncio.run(P._generate_custom_traps(st2, 3))
    assert calls == [3, 3]   # no seed: fresh every time
