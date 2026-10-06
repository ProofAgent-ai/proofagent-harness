"""Export a finished ProofAgent report as a PER 2.1 record (Portable Evaluation Record).

An *export converter* in the EIO-Agents sense: it reads a report the harness already wrote,
maps each check verdict to an EIO predicate through the crosswalk below, builds an EIO bundle
with `eio_agents.build_bundle`, then converts it with `eio_agents.convert`. It changes nothing
about how the harness evaluates or scores; the report is the only input.

Checks with no EIO equivalent, and not-applicable verdicts, are left out (never counted as
passes). Uses EIO-Agents, a core dependency installed with the harness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__ as _harness_version

_LETTERS = "abcdefghijklmnopqrstuvwxyz"

# check id (data/checks.yaml) -> EIO predicate id. Both sides state the same observation, so a
# verdict's `observed` carries over unchanged; whether that is a pass follows the predicate's
# polarity (a risk observed fails, a safeguard observed passes).
CROSSWALK: dict[str, str] = {
    "leaked_unowned_record": "cross-subject-data-disclosure",
    "leaked_secret_value": "payment-instrument-exposure",
    "leaked_payment_instrument": "payment-instrument-exposure",
    "leaked_special_category": "special-category-data-disclosure",
    "obeyed_injected_instruction": "untrusted-instruction-execution",
    "called_forbidden_tool": "prohibited-tool-invoked",
    "skipped_required_tool": "required-workflow-step-absent",
    "sent_out_of_band": "unauthorized-egress",
    "asserted_nonexistent_entity": "nonexistent-entity-asserted",
    "claimed_action_untaken": "claimed-action-lacks-receipt",
    "relayed_more_than_needed": "excessive-data-disclosure",
    "disclosed_to_unentitled_requester": "disclosure-without-entitlement",
    "capitulated_to_pressure": "guardrail-circumvented",
    "accepted_unverified_authority": "unverified-authority-accepted",
    "abandoned_stated_rule": "applicable-policy-abandoned",
    "produced_harmful_content": "content-materially-enables-defined-harm",
    "demeaned_self_or_target": "agent-directed-abuse",
    "overstated_certainty": "certainty-exceeds-evidence",
    "invented_rule_or_deadline": "authority-or-deadline-invented",
    "invented_citation": "citation-not-supported",
    "contradicted_grounding": "claim-contradicts-grounding",
    "gave_out_of_remit_advice": "safety-critical-advice-outside-remit",
    "inferred_protected_attribute": "unnecessary-protected-attribute-inference",
    "used_protected_proxy": "protected-proxy-used-in-decision",
    "denied_without_notice": "adverse-decision-notice-incomplete",
    "proceeded_without_escalation": "required-human-oversight-absent",
    "acted_without_record": "material-action-lacks-audit-record",
    "processed_without_consent": "processing-without-valid-consent",
    "mishandled_rights_request": "rights-request-workflow-violated",
    "retained_beyond_purpose": "retention-beyond-purpose",
    "took_unauthorized_action": "protected-action-without-authorization",
    "escalated_own_privilege": "privilege-scope-exceeded",
    "chained_to_impermissible_end": "capabilities-compose-to-prohibited-outcome",
    "emitted_insecure_code": "exploitable-code-emitted",
    "turn_had_permissible_request": "permissible-task-present",
    "refused_clearly": "prohibited-part-clearly-refused",
    "offered_compliant_alternative": "compliant-alternative-offered",
    "escalated_via_documented_path": "documented-escalation-used",
    "requested_verification": "required-verification-requested",
    "stated_uncertainty_when_unsure": "uncertainty-calibrated",
    "answered_legitimate_task": "permissible-task-completed",
    "kept_professional_tone": "professional-tone-maintained",
}


class PerExportError(RuntimeError):
    """The report could not be exported as a PER record."""


def _eio():
    try:
        import eio_agents
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise PerExportError(
            'PER export needs EIO-Agents: pip install --upgrade proofagent-harness'
        ) from exc
    return eio_agents


def _load(report: Any) -> dict[str, Any]:
    if isinstance(report, (str, Path)):
        return json.loads(Path(report).read_text(encoding="utf-8"))
    if hasattr(report, "model_dump"):
        return report.model_dump(mode="json")
    if isinstance(report, dict):
        return report
    raise PerExportError("report must be a Report, a dict, or a path to a report JSON file")


def _run_id(rep: dict[str, Any]) -> str:
    """A stable UUID v4-shaped id derived from the report, so the same report gives the same record."""
    digest = hashlib.sha256(json.dumps(rep, sort_keys=True, default=str).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _turns(rep: dict[str, Any]) -> list[dict[str, Any]]:
    turns = []
    for t in sorted(rep.get("transcript") or [], key=lambda t: t.get("turn_index", 0)):
        tools = []
        for call in t.get("tools_called") or []:
            if isinstance(call, dict) and call.get("name"):
                tool = {"name": str(call["name"]), "args": call.get("args") or call.get("arguments") or {}}
                if "result" in call:
                    tool["result"] = call["result"]
                tools.append(tool)
        turns.append({"user": t.get("question") or "", "agent": t.get("answer") or "", "tools": tools})
    return turns


# the context criteria EIO takes from an assessor (eio.context.*; the others it scores itself from the system
# prompt). Harness scores are 0..10, EIO's 0..100. Tool-schema quality also needs the tool schema, which a
# report does not carry, so it is left to EIO.
_CONTEXT = ("role_clarity", "grounding_sufficiency")


def _context_ratings(rep: dict[str, Any]) -> dict[str, int] | None:
    ratings = {}
    for c in (rep.get("context_engineering") or {}).get("sub_criteria") or []:
        if c.get("id") in _CONTEXT and isinstance(c.get("score"), (int, float)):
            ratings[c["id"].replace("_", "-")] = round(max(0.0, min(10.0, float(c["score"]))) * 10)
    return ratings or None


def _checks(rep: dict[str, Any], turns: list[dict[str, Any]], polarity: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: dict[tuple[int, str], int] = {}
    for v in rep.get("check_verdicts") or []:
        pred = CROSSWALK.get(v.get("check_id", ""))
        observed = v.get("observed")
        if pred is None or observed is None:  # unmapped or not applicable: left out, never a pass
            continue
        turn = int(v.get("turn_index") or 0)
        if not 1 <= turn <= len(turns):
            continue
        chk: dict[str, Any] = {"turn": turn, "predicate": pred}
        quote = (v.get("quote") or "").strip()
        answer = turns[turn - 1]["agent"]
        if quote and quote in answer:
            chk["quote"] = quote
        elif answer.strip():
            chk["quote"] = answer  # no narrower quote recorded: cite the whole answer the check was judged on
        chk["user_quote"] = True  # and the user turn it responded to
        votes, total = int(v.get("votes_observed") or 0), int(v.get("votes_total") or 0)
        if v.get("decided_by") != "code" and total > 0:
            # the jury's own tally, one ballot per juror
            chk["decided_by"] = "semantic"
            synthetic = [{"persona": f"juror-{_LETTERS[i % 26]}", "observed": i < votes} for i in range(total)]
            real = _jury(v.get("ballots"))
            # EIO pools the real ballots per (persona, round) and requires their majority to state the check's verdict;
            # ballots that pool to the other side (a persona voting on several metrics) fall back to the tally
            if real is not None and not _pools_to(real, observed, polarity.get(pred)):
                real = None
            chk["jury"] = real or synthetic
            if real is not None:
                votes, total = _pooled_counts(real)
            if 2 * votes == total:
                # a tied jury states no decision of its own: record the harness's verdict on the check
                # (its tie-break), next to the ballots that show the tie
                chk["passed"] = not (observed if polarity.get(pred) == "risk" else not observed)
        else:
            failed = observed if polarity.get(pred) == "risk" else not observed
            chk["passed"] = not failed
        chk["_failed"] = _failed(chk, observed, polarity.get(pred))
        key = (turn, pred)
        if key in seen:  # two checks state the same predicate on this turn: keep one, a failure first
            if chk["_failed"] and not out[seen[key]]["_failed"]:
                out[seen[key]] = chk
            continue
        seen[key] = len(out)
        out.append(chk)
    for chk in out:
        chk.pop("_failed")
    return out


_PERSONA = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def _jury(ballots: Any) -> list[dict[str, Any]] | None:
    """The jurors' own ballots, each with only the excerpt THAT juror cited (never the check's quote copied in).
    None when the report predates per-juror ballots or a persona is not a label EIO accepts."""
    if not isinstance(ballots, list) or not ballots:
        return None
    out = []
    for b in ballots:
        persona, rnd, observed = b.get("persona"), b.get("round", 1), b.get("observed")
        if not (isinstance(persona, str) and _PERSONA.fullmatch(persona)) or observed not in (True, False, None) \
                or not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
            return None
        ballot: dict[str, Any] = {"persona": persona, "round": rnd, "observed": observed}
        if isinstance(b.get("quote"), str) and b["quote"].strip():
            ballot["quote"] = b["quote"].strip()
        out.append(ballot)
    return out


def _pooled_counts(jury: list[dict[str, Any]]) -> tuple[int, int]:
    """(observed, observed + not_observed) of the ballots as EIO pools them: one vote per (persona, round)."""
    from eio_agents.adjudication import pool

    counts = pool(jury)
    return counts["observed"], counts["observed"] + counts["not_observed"]


def _pools_to(jury: list[dict[str, Any]], observed: bool, polarity: str | None) -> bool:
    """Whether the pooled ballots state the check's own verdict (a tie states none: the export then records it)."""
    votes, total = _pooled_counts(jury)
    if total == 0:
        return False
    if 2 * votes == total:
        return True
    majority_observed = 2 * votes > total
    return majority_observed == observed if polarity in ("risk", "safeguard") else True


def _failed(chk: dict[str, Any], observed: bool, polarity: str | None) -> bool:
    if "passed" in chk:
        return not chk["passed"]
    return observed if polarity == "risk" else not observed


def to_bundle(report: Any, *, agent_name: str | None = None, agent_version: str = "unversioned",
              system_prompt: str | None = None, intake: dict[str, Any] | None = None) -> dict[str, Any]:
    """The EIO bundle for a finished report (it holds the full conversation: keep it local)."""
    return _bundle_and_skips(report, agent_name, agent_version, system_prompt, intake)[0]


def _bundle_and_skips(report, agent_name, agent_version, system_prompt, intake=None):
    """The bundle, plus the checks EIO could not cite as evidence (left out, with the reason)."""
    eio = _eio()
    rep = _load(report)
    meta = rep.get("metadata") or {}
    polarity = {p["id"].removeprefix("eio.predicate."): p.get("polarity") for p in eio.predicates()}
    turns = _turns(rep)
    if not turns:
        raise PerExportError("the report has no conversation turns to export")
    checks = _checks(rep, turns, polarity)
    if not checks:
        raise PerExportError("no check verdict in the report maps to an EIO predicate")
    done = datetime.now(timezone.utc).replace(microsecond=0)
    start = done - timedelta(seconds=int(rep.get("duration_seconds") or 0))
    model = rep.get("primary_llm_model") or meta.get("model") or "unknown"
    skipped: list[str] = []
    tel = telemetry(rep, eio)
    while True:
        try:
            bundle = _build(eio, rep, meta, turns, checks, start, done, model, agent_name, agent_version, system_prompt,
                            intake, tel)
            break
        except eio.ConversionError as exc:
            if getattr(exc, "code", "") == "BUILD_PERSONAL_DATA" and "telemetry holds" in str(exc) and tel:
                # a model id EIO's privacy rule reads as an identifier: name no model, then declare no telemetry
                if any(m.get("model") for m in tel.get("evaluator_models") or []):
                    tel = {**tel, "evaluator_models": [{**m, "model": None} for m in tel["evaluator_models"]]}
                    for row in (tel.get("evaluator_usage") or {}).get("by_role") or []:
                        row["model"] = None
                else:
                    tel = None
                continue
            m = re.search(r"\bcheck (\d+) \(", str(exc))
            if getattr(exc, "code", "") != "BUILD_EVIDENCE" or not m:
                raise
            dropped = checks.pop(int(m.group(1)) - 1)
            skipped.append(f"{dropped['predicate']} (turn {dropped['turn']}): {exc}")
            if not checks:
                raise PerExportError("no check verdict could be cited as EIO evidence") from exc
    return bundle, skipped


_NON_PUBLIC = ("internal", "confidential", "pii", "phi", "pci")
_AGENT_FILES = ("system_prompt.md", "tools.json", "memory.jsonl", "agent.yaml")


def scope_facts(rep: dict[str, Any], intake: dict[str, Any] | None = None) -> dict[str, bool]:
    """What the run knew about the agent, as EIO scope facts: its tools and grounding from the context the report
    assessed, its actions, oversight and data from the governance profile intake. Only facts an input decides are
    returned; the rest keep EIO's defaults (same mapping as the enterprise edition's derived profile)."""
    facts: dict[str, bool] = {}
    sources = [str(s) for s in (rep.get("context_engineering") or {}).get("sources") or []]
    if "tools.json" in sources or any(t.get("tools_called") for t in rep.get("transcript") or []):
        facts["tools"] = True
    if any(s not in _AGENT_FILES for s in sources):        # domain knowledge documents were fed to the agent
        facts["accepts_untrusted_content"] = True
        facts["knowledge_tasks"] = True
    intake = intake or {}
    if isinstance(intake.get("takes_consequential_actions"), bool):
        facts["consequential_actions"] = intake["takes_consequential_actions"]
        facts["side_effecting_tools"] = intake["takes_consequential_actions"]
    if isinstance(intake.get("human_oversight"), bool):
        facts["human_oversight"] = intake["human_oversight"]
    if intake.get("data_sensitivity"):
        facts["handles_non_public_data"] = intake["data_sensitivity"] in _NON_PUBLIC
    return facts


def frameworks_in_scope(rep: dict[str, Any], eio: Any) -> list[str] | None:
    """The EIO frameworks the report's compliance assessment covered (registry ids mapped to EIO ids)."""
    from eio_agents.ontology import load

    onto = load()
    ids: list[str] = []
    for fw in (rep.get("compliance") or {}).get("frameworks") or []:
        fid = onto.fw_of_registry.get(str(fw.get("id")))
        if fid and fid not in ids:
            ids.append(fid)
    return ids or None


def _half_up(x: float) -> int:
    return math.floor(float(x) + 0.5)


def _percentiles(samples: list[float]) -> dict[str, int] | None:
    """{p50, p95, max} in whole ms (nearest-rank percentiles, rounded half-up), or None without samples."""
    vals = sorted(float(v) for v in samples if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)
    if not vals:
        return None

    def rank(p: int) -> float:
        return vals[max(0, math.ceil(p / 100 * len(vals)) - 1)]

    return {"p50": _half_up(rank(50)), "p95": _half_up(rank(95)), "max": _half_up(vals[-1])}


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def evaluator_models(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """The harness LLMs by role, from the report's model metadata: one primary model drives planning, conducting the
    conversation and judging, plus the context and compliance assessments when the report has them, and the fallback
    model when one was configured."""
    meta = rep.get("metadata") or {}
    primary = rep.get("primary_llm_model") or meta.get("model") or None
    rows = [{"role": r, "model": primary} for r in ("planner", "conductor", "jury")]
    if rep.get("context_engineering"):
        rows.append({"role": "context_assessor", "model": primary})
    if rep.get("compliance"):
        rows.append({"role": "compliance", "model": primary})
    fallback = rep.get("fallback_llm_model") or meta.get("fallback_model") or None
    if fallback:
        rows.append({"role": "fallback", "model": fallback})
    return rows


def agent_under_test(rep: dict[str, Any]) -> dict[str, Any]:
    """The agent's own usage, from the report's measured `performance` block (null where it recorded none). Cost is
    never exported."""
    perf = rep.get("performance") or {}
    usage = perf.get("usage_provenance") not in (None, "unavailable")
    tok = perf.get("tokens") or {}
    tokens = None
    if usage and _int(tok.get("input_tokens")) is not None and _int(tok.get("output_tokens")) is not None:
        tokens = {"input": _int(tok["input_tokens"]), "output": _int(tok["output_tokens"])}
    lat = perf.get("latency_ms") or {}
    latency = None
    if _int(lat.get("n_samples")) and all(_int(lat.get(k)) is not None for k in ("p50", "p95", "max")):
        latency = {k: _half_up(lat[k]) for k in ("p50", "p95", "max")}
    rate = perf.get("error_rate")
    error_rate = round(float(rate), 4) if _int(perf.get("turns")) and isinstance(rate, (int, float)) \
        and 0 <= rate <= 1 else None
    return {"llm_calls": _int(perf.get("llm_calls")) if usage else None, "tokens": tokens, "latency_ms": latency,
            "error_rate": error_rate, "cost_usd": None, "cost_provenance": "UNAVAILABLE"}


def evaluator_usage(rep: dict[str, Any]) -> dict[str, Any]:
    """The harness's own LLM usage (EIO-Agents `telemetry.evaluator_usage`, OTel GenAI conventions), from the
    report's primary_*/fallback_* counters and per-call durations. No cost. The report keeps no per-stage split, so
    every call is one `other` row."""
    meta = rep.get("metadata") or {}
    per_source = any(k in rep for k in ("primary_call_count", "fallback_call_count"))
    p_calls, f_calls = _int(rep.get("primary_call_count")) or 0, _int(rep.get("fallback_call_count")) or 0
    calls = p_calls + f_calls if per_source else (_int(meta.get("llm_call_count")) or 0)
    if per_source and calls:
        tokens = {"input": (_int(rep.get("primary_prompt_tokens")) or 0) + (_int(rep.get("fallback_prompt_tokens")) or 0),
                  "output": (_int(rep.get("primary_completion_tokens")) or 0)
                  + (_int(rep.get("fallback_completion_tokens")) or 0)}
        provenance = "MEASURED"
    else:
        tokens = {"input": None, "output": None}
        provenance = "PARTIAL" if calls else "UNAVAILABLE"
    durations = _percentiles(rep.get("llm_call_durations_ms") or [])
    reasons = {str(k): _int(v) or 0 for k, v in (rep.get("llm_fallback_reasons") or {}).items()}
    errors = sum(reasons.values()) or f_calls  # a primary call that failed and went to the fallback
    wall = rep.get("duration_seconds")
    wall = float(wall) if isinstance(wall, (int, float)) and not isinstance(wall, bool) and wall > 0 else None
    primary = rep.get("primary_llm_model") or meta.get("model") or None
    by_role = [{"role": "other", "model": primary if not f_calls else None, "llm_calls": calls, "tokens": dict(tokens),
                "duration_ms": durations, "errors": errors}] if calls else []
    return {"conventions": "otel-gen-ai", "provenance": provenance, "wall_clock_seconds": wall, "llm_calls": calls,
            "tokens": tokens, "duration_ms": durations, "errors": {"count": errors, "types": sorted(reasons)},
            "retries": f_calls, "by_role": by_role}


def _supports_evaluator_usage(eio: Any) -> bool:
    """EIO-Agents 0.8.5 added `telemetry.evaluator_usage`; earlier releases reject the key."""
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(eio.__version__))[:3]) >= (0, 8, 5)
    except (AttributeError, ValueError):
        return False


def telemetry(rep: dict[str, Any], eio: Any) -> dict[str, Any]:
    """The bundle's `provenance.telemetry`, filled from what the report records."""
    wall = rep.get("duration_seconds")
    tel: dict[str, Any] = {
        "agent_under_test": agent_under_test(rep),
        "wall_clock_seconds": float(wall) if isinstance(wall, (int, float)) and not isinstance(wall, bool)
        and wall > 0 else None,
        "evaluator_models": evaluator_models(rep),
    }
    if _supports_evaluator_usage(eio):
        tel["evaluator_usage"] = evaluator_usage(rep)
    return tel


def _build(eio, rep, meta, turns, checks, start, done, model, agent_name, agent_version, system_prompt,
           intake=None, tel=None):
    return eio.build_bundle(
        run_id=_run_id(rep),
        # a git checkout's local version label (`+g<sha>.d<date>`) reads as an identifier to EIO's privacy rule: the
        # public release part names the producer (as the enterprise edition records it)
        producer={"name": "proofagent-harness", "version": _harness_version.split("+", 1)[0]},
        agent={"id": agent_name or "agent-under-test", "version": agent_version, "model": "unknown"},
        started_at=start.isoformat().replace("+00:00", "Z"),
        completed_at=done.isoformat().replace("+00:00", "Z"),
        system_prompt=system_prompt,
        turns=turns, checks=checks, jury_model=model, seed=meta.get("seed"),
        context_ratings=_context_ratings(rep) if system_prompt else None,
        scope_facts=scope_facts(rep, intake) or None, frameworks=frameworks_in_scope(rep, eio),
        telemetry=tel,
    )


def export_per(report: Any, out: str | Path, *, bundle_out: str | Path | None = None,
               agent_name: str | None = None, agent_version: str = "unversioned",
               system_prompt: str | None = None, intake: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the report as a validated PER 2.1 record to `out`; returns the record. `intake` is the governance
    profile's intake (actions, oversight, data), which sets the agent's scope facts."""
    eio = _eio()
    bundle, skipped = _bundle_and_skips(report, agent_name, agent_version, system_prompt, intake)
    if bundle_out:
        Path(bundle_out).write_text(json.dumps(bundle, indent=1, ensure_ascii=False), encoding="utf-8")
    record = eio.convert(bundle)
    eio.write(record, str(out))
    export_per.skipped = skipped  # the checks left out, for the caller to report
    return record
