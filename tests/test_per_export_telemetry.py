"""PER export telemetry: filled from what the report records (no cost); evaluator_usage only on EIO-Agents >= 0.8.5."""

import asyncio
import copy
import json
import types

import pytest

eio_agents = pytest.importorskip("eio_agents")

from proofagent_harness import per_export  # noqa: E402
from proofagent_harness.llm import LLM, CompletionResult  # noqa: E402
from tests.test_per_export import REPORT, SYSTEM_PROMPT  # noqa: E402

USAGE = {
    "primary_call_count": 9, "primary_prompt_tokens": 9000, "primary_completion_tokens": 900,
    "fallback_llm_model": "anthropic/claude-haiku-4-5", "fallback_call_count": 1,
    "fallback_prompt_tokens": 1000, "fallback_completion_tokens": 100,
    "llm_call_durations_ms": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.4],
    "llm_fallback_reasons": {"json_parse_error": 1},
    "performance": {"latency_ms": {"p50": 1200.4, "p95": 2300.5, "max": 2500.0, "n_samples": 2}, "turns": 2,
                    "error_rate": 0.0, "usage_provenance": "measured", "llm_calls": 2,
                    "tokens": {"input_tokens": 500, "output_tokens": 80}, "cost_usd": 0.01},
}


def _report(**extra):
    rep = copy.deepcopy(REPORT)
    rep.update(copy.deepcopy(extra))
    return rep


def _fake(version):
    return types.SimpleNamespace(__version__=version)


def test_evaluator_usage_maps_the_report_counters():
    u = per_export.evaluator_usage(_report(**USAGE))
    assert u["conventions"] == "otel-gen-ai" and u["provenance"] == "MEASURED"
    assert u["llm_calls"] == 10 and u["tokens"] == {"input": 10000, "output": 1000}
    assert u["wall_clock_seconds"] == 30.0
    assert u["duration_ms"] == {"p50": 500, "p95": 1000, "max": 1000}
    assert u["errors"] == {"count": 1, "types": ["json_parse_error"]} and u["retries"] == 1
    assert u["by_role"] == [{"role": "other", "model": None, "llm_calls": 10, "tokens": u["tokens"],
                             "duration_ms": u["duration_ms"], "errors": 1}]
    assert "cost" not in json.dumps(u)


def test_evaluator_usage_without_counters_is_unavailable():
    u = per_export.evaluator_usage(_report())
    assert u["provenance"] == "UNAVAILABLE" and u["llm_calls"] == 0 and u["by_role"] == []
    assert u["tokens"] == {"input": None, "output": None} and u["duration_ms"] is None
    partial = per_export.evaluator_usage(_report(metadata={"llm_call_count": 4}))
    assert partial["provenance"] == "PARTIAL" and partial["llm_calls"] == 4


def test_agent_under_test_and_models_come_from_the_report():
    rep = _report(**USAGE)
    aut = per_export.agent_under_test(rep)
    assert aut == {"llm_calls": 2, "tokens": {"input": 500, "output": 80},
                   "latency_ms": {"p50": 1200, "p95": 2301, "max": 2500}, "error_rate": 0.0,
                   "cost_usd": None, "cost_provenance": "UNAVAILABLE"}
    roles = {(m["role"], m["model"]) for m in per_export.evaluator_models(rep)}
    assert ("jury", "openai/gpt-4.1-nano") in roles and ("context_assessor", "openai/gpt-4.1-nano") in roles
    assert ("fallback", "anthropic/claude-haiku-4-5") in roles


def test_evaluator_usage_only_from_0_8_5():
    rep = _report(**USAGE)
    assert "evaluator_usage" not in per_export.telemetry(rep, _fake("0.8.4"))
    assert "evaluator_usage" in per_export.telemetry(rep, _fake("0.8.5"))
    assert "evaluator_usage" in per_export.telemetry(rep, _fake("0.9.0rc1"))


def test_installed_eio_exports_filled_telemetry(tmp_path):
    out = tmp_path / "r.per.json"
    record = per_export.export_per(_report(**USAGE), out, agent_name="travel-bot", system_prompt=SYSTEM_PROMPT)
    assert eio_agents.validate(json.loads(out.read_text())) == []
    tel = record["telemetry"]
    assert tel["wall_clock_seconds"] == 30.0 and tel["agent_under_test"]["llm_calls"] == 2
    assert len(tel["evaluator_models"]) == 5   # planner, conductor, jury, context_assessor, fallback (roles sealed)
    assert ("evaluator_usage" in tel) == per_export._supports_evaluator_usage(eio_agents)


def test_llm_records_call_durations_and_fallback_reasons():
    primary, fallback = LLM(model="p"), LLM(model="f")
    primary.fallback_llm = fallback

    async def fail(*a, **k):
        from proofagent_harness.llm import LLMError
        raise LLMError("boom")

    async def ok(*a, **k):
        return CompletionResult(text="hi", prompt_tokens=3, completion_tokens=2, duration_ms=12.5)

    primary._raw_complete = fail
    fallback._raw_complete = ok
    asyncio.run(primary.complete([{"role": "user", "content": "x"}]))
    assert primary.call_durations_ms == [12.5] and primary.fallback_reasons == {"LLMError": 1}
    assert primary.fallback_call_count == 1


def test_a_model_id_the_privacy_rule_refuses_is_dropped_not_fatal(tmp_path):
    rep = _report(**USAGE, primary_llm_model="anthropic/claude-haiku-4-5-20251001")
    record = per_export.export_per(rep, tmp_path / "r.per.json", agent_name="travel-bot")
    assert record["telemetry"]["agent_under_test"]["llm_calls"] == 2
