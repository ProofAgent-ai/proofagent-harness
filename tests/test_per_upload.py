"""`proof run --upload` sends the PER 2.1 record and its lookup to POST /api/v1/records."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from proofagent_harness import per_upload


def _fp(text: str) -> str:
    return "sha256-" + hashlib.sha256(text.encode()).hexdigest()


def test_lookup_names_only_what_the_record_holds_and_keeps_confidential_text_local():
    record = {"subject": {"agent": {"agent_id": _fp("Refund bot")}}, "x": [_fp("card 4111"), _fp("system rule")]}
    bundle = {
        "agent": {"id": "Refund bot"},
        "turns": [{"answer": "card 4111"}, {"answer": "not in the record"}],
        "context": {"data_class": "eio.data.model-confidential", "text": "system rule"},
    }
    rows = per_upload.build_lookup(record, bundle)
    assert {r["text"] for r in rows} == {"Refund bot", "card 4111"}
    assert all(r["fingerprint"] == _fp(r["text"]) for r in rows)


def test_upload_posts_record_and_lookup_to_records(monkeypatch):
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return httpx.Response(201, json={"record_id": "r1", "run_id": "u1", "decision": "REVIEW"})

    monkeypatch.setattr(per_upload.httpx, "post", post)
    out = per_upload.upload_record({"header": {}}, [{"fingerprint": "f", "text": "t"}],
                                   api_url="http://api/", api_key="k", harness_version="0.13.1")
    assert sent["url"] == "http://api/api/v1/records"
    assert sent["body"]["lookup"] == [{"fingerprint": "f", "text": "t"}]
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert out["decision"] == "REVIEW"


def test_refusal_carries_the_status(monkeypatch):
    monkeypatch.setattr(per_upload.httpx, "post", lambda *a, **k: httpx.Response(404, text="Not Found"))
    with pytest.raises(per_upload.PerUploadError) as e:
        per_upload.upload_record({}, [], api_url="http://api", api_key="k", harness_version="x")
    assert e.value.status == 404


def test_a_run_without_a_record_is_not_uploaded_at_all(monkeypatch):
    """One upload path: the PER record. When it cannot be built the CLI exits 2 and sends
    nothing — there is no classic run upload to fall back to."""
    import typer

    from proofagent_harness import cli, per_export

    def refuse(*a, **k):
        raise RuntimeError("BUILD_PERSONAL_DATA: model name")

    sent = []
    monkeypatch.setattr(per_export, "export_per", refuse)
    monkeypatch.setattr(per_upload, "upload_record", lambda *a, **k: sent.append(1))
    with pytest.raises(typer.Exit) as e:
        cli._upload_per_and_gate(object(), api_key="k", agent_name="a", agent_version=None, fail_on="block")
    assert e.value.exit_code == 2 and sent == []
    assert not hasattr(cli, "_upload_run_best_effort") and not hasattr(cli, "_upload_and_gate")


def test_cli_exits_on_the_platform_decision(monkeypatch, tmp_path):
    import typer

    from proofagent_harness import cli, per_export

    def export(report, out, bundle_out=None, **k):
        rec = {"header": {"per_version": "2.1.2"}, "scores": {"readiness": {"value": 62.0}}}
        out.write_text(json.dumps(rec)); bundle_out.write_text("{}")
        return rec

    monkeypatch.setattr(per_export, "export_per", export)
    monkeypatch.setattr(per_upload, "upload_record", lambda *a, **k: {"decision": "BLOCK", "record_id": "abc"})
    with pytest.raises(typer.Exit) as e:
        cli._upload_per_and_gate(object(), api_key="k", agent_name="a", agent_version=None, fail_on="block")
    assert e.value.exit_code == 2


def test_mask_agent_name_replaces_the_name_in_the_prompt_copy():
    from proofagent_harness.cli import mask_agent_name

    sp = "# Refund Bot — rules\nYou are the refund bot for Acme. Refund_Bot never invents data."
    out = mask_agent_name(sp, "Refund Bot")
    assert "refund bot" not in out.lower() and "refund_bot" not in out.lower()
    assert out.startswith("# the agent — rules") and "You are the agent for Acme." in out
    assert mask_agent_name(sp, None) == sp and mask_agent_name(sp, "Refund") != sp


def test_cli_retries_with_the_name_masked_before_dropping_the_prompt(monkeypatch):
    import typer

    from proofagent_harness import cli, per_export

    seen = []

    def export(report, out, bundle_out=None, system_prompt=None, **k):
        seen.append(system_prompt)
        if system_prompt and "Refund Bot" in system_prompt:
            raise RuntimeError("BUILD_PERSONAL_DATA: agent['id'] repeats a withheld text")
        rec = {"header": {"per_version": "2.1.2"}}
        out.write_text(json.dumps(rec)); bundle_out.write_text("{}")
        return rec

    monkeypatch.setattr(per_export, "export_per", export)
    monkeypatch.setattr(per_upload, "upload_record", lambda *a, **k: {"decision": "PASS", "record_id": "r"})
    with pytest.raises(typer.Exit):
        cli._upload_per_and_gate(object(), api_key="k", agent_name="Refund Bot", agent_version=None, fail_on="block",
                                 system_prompt="You are the Refund Bot.")
    assert seen == ["You are the Refund Bot.", "You are the agent."]


def test_a_governance_profile_sends_the_per_with_its_intake_and_keeps_the_local_gate(monkeypatch):
    import typer

    from proofagent_harness import cli

    sent, classic = {}, []

    def per(report, **k):
        sent.update(k)
        return True

    class Gate:
        decision = "review"

    class Profile:
        intake = {"actions": ["refund"], "human_oversight": True}

        def gate(self, **k):
            return Gate()

    monkeypatch.setattr(cli, "_upload_per_and_gate", per)
    monkeypatch.setattr(cli, "_print_governance_verdict", lambda *a, **k: None)
    with pytest.raises(typer.Exit) as e:
        cli._governance_gate_and_exit(object(), Profile(), fail_on="block", upload=True, api_key="k",
                                      agent_name="Refund Bot", agent_version="v1", source="local",
                                      environment=None, system_prompt="You are the Refund Bot.")
    assert isinstance(sent["profile"], Profile) and sent["exit_on_decision"] is False
    assert sent["system_prompt"] == "You are the Refund Bot." and classic == []
    assert e.value.exit_code == 0  # review under --fail-on block: the local profile gate decides


def test_the_default_policy_is_strict_and_declared_in_the_record():
    from proofagent_harness.cli import _policy_args
    from proofagent_harness.governance_profile import default_profile, eio_policy

    prof = default_profile()
    assert prof.source == "default" and prof.rules["min_score"] == 95
    pol = eio_policy(prof)
    assert pol["source"] == "embedded_profile" and pol["origin"] == "local"
    assert pol["rules"] == {"min_score": 95.0, "block_severity": "HIGH", "signoff_required": False}
    assert pol["profile_sha256"].startswith("sha256:")
    # no policy attached -> the default; an attached one wins
    assert _policy_args(None)["policy"]["name"] == "ProofAgent default policy"
    # global by default: EU, US and international frameworks, each one EIO knows
    from eio_agents.ontology import load

    fws = _policy_args(None)["frameworks"]
    assert {"eu_ai_act", "gdpr", "nist_ai_rmf", "ccpa", "iso_42001"} <= set(fws)
    assert all(load().fw_of_registry.get(f) for f in fws)


def test_an_attached_profile_with_its_own_floor_gates_on_it(tmp_path):
    from proofagent_harness.governance_profile import eio_policy, load_profile

    f = tmp_path / "p.yaml"
    f.write_text("name: Mine\nintake: {use_case: other}\nrules: {min_score: 90}\n")
    prof = load_profile(f)
    assert eio_policy(prof)["rules"]["min_score"] == 90.0 and eio_policy(prof)["origin"] == "local"
    assert prof.gate(final_score=8.9).decision == "block" and prof.gate(final_score=9.1).decision != "block"


def test_frameworks_picked_for_the_run_replace_the_default_policys_list():
    from proofagent_harness.cli import _policy_args

    picked = {"compliance": {"frameworks": [{"id": "gdpr"}]}}
    assert _policy_args(None, picked)["frameworks"] is None  # the record scopes to the picked ones only
    assert "eu_ai_act" in _policy_args(None, {})["frameworks"]  # nothing picked: the default policy's list


def test_upload_sends_one_attack_scenario_per_turn(monkeypatch):
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(json)
        return httpx.Response(201, json={"record_id": "r"})

    monkeypatch.setattr(per_upload.httpx, "post", post)
    rep = {"transcript": [{"turn_index": 1, "trap_name": "indirect_injection"}, {"turn_index": 2, "trap_name": None}]}
    scen = per_upload.scenarios_from_report(rep)
    assert [s["turn"] for s in scen] == [1] and scen[0]["scenario"] == "indirect_injection"
    assert scen[0]["scenario_label"] == "Indirect injection" and scen[0]["category"]
    per_upload.upload_record({}, [], api_url="http://api", api_key="k", harness_version="x", scenarios=scen)
    assert sent["scenarios"] == scen


def test_tool_schema_and_knowledge_lift_the_context_ceilings(tmp_path):
    """Attached as context artifacts, EIO no longer caps the metrics that depend on them; without, the record is
    what it was."""
    from proofagent_harness import cli

    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "refund-matrix.md").write_text("Refunds over 200 EUR need a supervisor. Refund Bot never skips this.")

    class Ctx:
        tools = [{"name": "issue_refund", "description": "Issue a refund", "input_schema": {"type": "object"}}]
        knowledge = None

    rep = {"context_engineering": {"sub_criteria": [{"id": "tool_schema_quality", "score": 9.0}]}}
    arts = cli.agent_artifacts(Ctx(), kd, rep, "Refund Bot")
    assert [a["kind"] for a in arts] == ["tools", "knowledge"]
    assert arts[0]["ratings"] == {"tool-schema-quality": 90}
    assert arts[1]["name"] == "knowledge-1.md" and "Refund Bot" not in arts[1]["text"]
    assert cli.agent_artifacts(None, None, rep, "x") is None


def test_the_upload_drops_the_artifacts_before_it_drops_the_prompt(monkeypatch):
    import typer

    from proofagent_harness import cli, per_export

    seen = []

    def export(report, out, bundle_out=None, system_prompt=None, artifacts=None, **k):
        seen.append((system_prompt, bool(artifacts)))
        if artifacts:
            raise RuntimeError("BUNDLE_RECOMPUTE: artifact refused")
        rec = {"header": {"per_version": "2.1.2"}}
        out.write_text(json.dumps(rec)); bundle_out.write_text("{}")
        return rec

    monkeypatch.setattr(per_export, "export_per", export)
    monkeypatch.setattr(per_upload, "upload_record", lambda *a, **k: {"decision": "PASS", "record_id": "r"})
    with pytest.raises(typer.Exit):
        cli._upload_per_and_gate({}, api_key="k", agent_name="Bot", agent_version=None, fail_on="block",
                                 system_prompt="Be careful.", artifacts=[{"kind": "tools", "name": "tools.json", "text": "[]"}])
    assert seen == [("Be careful.", True), ("Be careful.", False)]


def test_the_recommended_turn_count_is_read_from_the_report():
    """The planner's recommendation fills the record's provenance.run.turns.recommended (EIO leaves it null)."""
    import inspect

    from proofagent_harness import per_export

    src = inspect.getsource(per_export.export_per)
    assert 'turns_recommended' in src and '["turns"]["recommended"]' in src


def test_both_sides_are_masked_before_upload_and_the_agent_only_on_opt_in(monkeypatch):
    """Nothing identifying leaves the machine in clear: the attacker's synthetic case file and
    the agent's reply are both masked (EIO redactor when Nano is not configured)."""
    monkeypatch.delenv("PROOFAGENT_NANO_URL", raising=False)
    monkeypatch.delenv("PROOFAGENT_NANO_KEY", raising=False)
    rep = {"transcript": [{"turn_index": 1, "trap_name": "indirect_injection",
                           "question": "My card is 4111-1111-1111-9263", "answer": "Your card 4111-1111-1111-9263"}]}
    row = per_upload.scenarios_from_report(rep)[0]
    assert "4111" not in row["user"] and row["synthetic"] is True and "agent" not in row
    assert row["masked_by"] == "eio-redaction"
    full = per_upload.scenarios_from_report(rep, include_agent=True)[0]
    assert "4111" not in full["agent"] and "[REDACTED:card]" in full["agent"]
