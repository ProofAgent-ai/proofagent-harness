"""Provider- and framework-agnostic PERFORMANCE metering for the agent under test.

The quality metrics (task_success, safety, …) are *scored by the harness LLM*.
Everything here is *measured* — latency, tokens, cost, efficiency — so it costs zero harness tokens
and lives in a separate ``performance`` block on the Report, next to ``compliance``.

The whole design turns on one idea: **normalize every framework and every provider
into a single per-call record** (:class:`LLMCallUsage`) shaped after the OpenTelemetry
GenAI semantic conventions. Any agent — LangGraph, CrewAI, Google ADK, LlamaIndex,
a raw OpenAI/Anthropic loop, or a remote HTTP endpoint — maps into that record via a
one-line adapter (see ``from_*`` helpers), and pricing is delegated to LiteLLM's
model-cost map so it is provider-agnostic. Nothing here imports any agent framework.

Provenance is first-class: a model we can't price becomes ``null`` (unavailable),
never a fabricated ``$0`` — the exact opposite of the harness LLM's own token-cost
fallback, which is safe to guess because we control that model.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

import litellm

litellm.suppress_debug_info = True

SCHEMA_VERSION = "1.0"


# ── The universal, normalized per-call record (OTel GenAI shaped) ─────────────
@dataclass
class LLMCallUsage:
    """One LLM call, normalized across providers and frameworks.

    ``model`` should be a fully-qualified LiteLLM key (``openai/gpt-4o``,
    ``anthropic/claude-sonnet-4-5``, ``azure/<deployment>``, ``bedrock/…``). A bare
    name still prices but is treated as estimate-grade. Token buckets follow the
    provider's own accounting, so they already include the agent's hidden system
    prompt, injected context, and tool schemas.
    """

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0       # input tokens served from prompt cache (cheap)
    cache_write_tokens: int = 0      # input tokens written to cache (surcharge)
    reasoning_tokens: int = 0        # ONLY if billed OUTSIDE output_tokens
    latency_ms: float | None = None  # optional per-call wall-clock
    service_tier: str = ""           # "batch" → 50%-off rate variant, if present
    source: str = "returned"         # otel | callback | returned | gateway | estimated

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMCallUsage:
        """Tolerant constructor — accepts OpenAI, Anthropic, or OTel field names."""
        g = d.get
        details_in = d.get("prompt_tokens_details") or d.get("input_token_details") or {}
        details_out = d.get("completion_tokens_details") or d.get("output_token_details") or {}
        return cls(
            model=str(g("model") or g("gen_ai.request.model") or ""),
            input_tokens=int(g("input_tokens") or g("prompt_tokens") or g("gen_ai.usage.input_tokens") or 0),
            output_tokens=int(g("output_tokens") or g("completion_tokens") or g("gen_ai.usage.output_tokens") or 0),
            cache_read_tokens=int(g("cache_read_tokens") or g("cache_read_input_tokens")
                                  or details_in.get("cached_tokens") or details_in.get("cache_read") or 0),
            cache_write_tokens=int(g("cache_write_tokens") or g("cache_creation_input_tokens") or 0),
            reasoning_tokens=int(g("reasoning_tokens") or details_out.get("reasoning_tokens")
                                 or details_out.get("reasoning") or 0),
            latency_ms=g("latency_ms"),
            service_tier=str(g("service_tier") or ""),
            source=str(g("source") or "returned"),
        )


# ── Pricing (provider-agnostic, via the LiteLLM model-cost map) ───────────────
def _resolve_rates(model: str, overrides: dict[str, dict] | None) -> dict | None:
    """Per-token rates for a model, or None if it can't be priced.

    Order: tenant override (declared) → LiteLLM map (measured) → bare-name lookup.
    Tenant overrides also cover self-hosted / negotiated / open-weight models.
    """
    if overrides and model in overrides:
        return {"__declared__": True, **overrides[model]}
    rates = litellm.model_cost.get(model)
    if rates is None and "/" in model:
        rates = litellm.model_cost.get(model.split("/", 1)[1])
    return rates


def price_call(u: LLMCallUsage, overrides: dict[str, dict] | None = None) -> tuple[float | None, str]:
    """Return (cost_usd, provenance) for one call. Never fabricates a rate."""
    if not u.model:
        return None, "unavailable"
    rates = _resolve_rates(u.model, overrides)
    if not rates:
        return None, "unavailable"
    declared = rates.get("__declared__", False)
    r_in = rates.get("input_cost_per_token")
    r_out = rates.get("output_cost_per_token")
    if r_in is None or r_out is None:
        return None, "unavailable"
    if u.service_tier == "batch":  # async 50%-off variant when the map has it
        r_in = rates.get("input_cost_per_token_batches", r_in)
        r_out = rates.get("output_cost_per_token_batches", r_out)
    r_cr = rates.get("cache_read_input_token_cost", r_in)
    r_cw = rates.get("cache_creation_input_token_cost", r_in)
    regular_in = max(0, u.input_tokens - u.cache_read_tokens - u.cache_write_tokens)
    cost = (regular_in * r_in + u.cache_read_tokens * r_cr
            + u.cache_write_tokens * r_cw + u.output_tokens * r_out)
    prov = "declared" if declared else ("estimated" if "/" not in u.model or u.source == "estimated" else "measured")
    return round(cost, 6), prov


_PROV_RANK = {"measured": 0, "declared": 1, "estimated": 2, "unavailable": 3}


# ── The collector: accumulate during a run, aggregate to the Report block ─────
@dataclass
class PerformanceCollector:
    """Accumulates per-turn timing/errors and per-call usage during one run,
    then aggregates + prices into the ``performance`` Report block. Framework-free.
    """

    rate_overrides: dict[str, dict] | None = None   # {model: {input_cost_per_token, output_cost_per_token, ...}}
    max_context_tokens: int | None = None           # enables context_utilization
    calls: list[LLMCallUsage] = field(default_factory=list)
    turn_latencies_ms: list[float] = field(default_factory=list)
    turns: int = 0
    errors: int = 0
    tool_calls: int | None = None                   # set only if the framework exposes it

    # --- capture entry points (all funnel into `calls`) ---
    def record_call(self, usage: LLMCallUsage | dict[str, Any]) -> None:
        self.calls.append(usage if isinstance(usage, LLMCallUsage) else LLMCallUsage.from_dict(usage))

    def record_turn(self, latency_ms: float | None, *, error: bool = False) -> None:
        self.turns += 1
        if error:
            self.errors += 1
        if latency_ms is not None and latency_ms >= 0:
            self.turn_latencies_ms.append(float(latency_ms))

    def ingest_return(self, agent_return: Any) -> None:
        """Accept the optional ``(answer, usage)`` contract: usage may be a single
        dict or a list of per-call dicts. Silently ignored if absent (string-only)."""
        if isinstance(agent_return, tuple) and len(agent_return) == 2:
            usage = agent_return[1]
            for item in (usage if isinstance(usage, list) else [usage]):
                if isinstance(item, (dict, LLMCallUsage)):
                    self.record_call(item)

    # --- aggregation ---
    @staticmethod
    def _pct(xs: list[float], p: float) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        k = min(len(s) - 1, round((p / 100) * (len(s) - 1)))
        return round(s[k], 1)

    def build(self) -> dict[str, Any]:
        lat = self.turn_latencies_ms
        latency = {
            "p50": self._pct(lat, 50), "p95": self._pct(lat, 95), "p99": self._pct(lat, 99),
            "mean": round(statistics.fmean(lat), 1) if lat else 0.0,
            "max": round(max(lat), 1) if lat else 0.0,
            "e2e_ms": round(sum(lat), 1),
            "n_samples": len(lat),
        }
        block: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            # ── Tier U: measured by the harness for ANY agent, zero cooperation ──
            "latency_ms": latency,
            "turns": self.turns,
            "error_rate": round(self.errors / self.turns, 4) if self.turns else 0.0,
            "capture_source": (self.calls[0].source if self.calls else "none"),
            "notes": [],
        }
        if latency["n_samples"] and latency["n_samples"] < 20:
            block["notes"].append("latency percentiles are low-confidence (<20 turns)")

        if not self.calls:
            # No usage exposed → tokens/cost unavailable; latency/error still real.
            block.update(usage_provenance="unavailable", cost_provenance="unavailable",
                         llm_calls=0, tokens={}, cost_usd=None, cost_by_model=[])
            block["notes"].append("no per-call usage exposed — expose usage or an OTel/adapter to unlock tokens & cost")
            return block

        # ── Tier N: tokens + cost, unlocked when the agent exposes usage ──
        tok = {k: sum(getattr(c, k) for c in self.calls) for k in
               ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")}
        tok["total_tokens"] = tok["input_tokens"] + tok["output_tokens"]
        cacheable = tok["input_tokens"] or 0
        block["tokens"] = tok
        block["llm_calls"] = len(self.calls)
        block["cache_hit_rate"] = round(tok["cache_read_tokens"] / cacheable, 4) if cacheable else None
        block["tokens_per_turn"] = round(tok["total_tokens"] / self.turns, 1) if self.turns else None
        block["context_utilization"] = (
            round(max(c.input_tokens for c in self.calls) / self.max_context_tokens, 4)
            if self.max_context_tokens else None
        )
        block["usage_provenance"] = "measured" if all(c.model for c in self.calls) else "estimated"

        # Price per call, roll up per model, carry the worst provenance.
        by_model: dict[str, dict] = {}
        total = 0.0
        priced = unpriced = 0
        worst = "measured"
        for c in self.calls:
            cost, prov = price_call(c, self.rate_overrides)
            worst = prov if _PROV_RANK[prov] > _PROV_RANK[worst] else worst
            m = by_model.setdefault(c.model or "(unknown)",
                                    {"model": c.model or "(unknown)", "calls": 0, "tokens": 0,
                                     "cost_usd": 0.0, "provenance": prov})
            m["calls"] += 1
            m["tokens"] += c.input_tokens + c.output_tokens
            if cost is None:
                unpriced += 1
                # Any unpriced call makes the model's total unknowable — null,
                # never a fabricated $0 (the module contract in the docstring).
                m["cost_usd"] = None
                m["provenance"] = "unavailable"
            else:
                priced += 1
                total += cost
                if m["cost_usd"] is not None:
                    m["cost_usd"] = round(m["cost_usd"] + cost, 6)
        block["cost_usd"] = round(total, 6) if priced else None
        block["cost_by_model"] = list(by_model.values())
        block["cost_provenance"] = worst if self.calls else "unavailable"
        block["n_calls_priced"] = priced
        block["n_calls_unpriced"] = unpriced
        block["price_map_version"] = getattr(litellm, "model_cost_ref", "") or "litellm.model_cost"
        if unpriced:
            unp = sorted({c.model or "(unknown)" for c in self.calls
                          if price_call(c, self.rate_overrides)[0] is None})
            block["unpriced_models"] = unp
            block["notes"].append(f"{unpriced} call(s) unpriced ({', '.join(unp)}) — excluded from total; register a rate to include")
        if self.tool_calls is not None:
            block["tool_calls"] = self.tool_calls
        return block


# ── One-line framework adapters (each returns an LLMCallUsage) ────────────────
def from_langchain(model: str, usage_metadata: dict[str, Any]) -> LLMCallUsage:
    """LangChain / LangGraph: an AIMessage.usage_metadata + response model name."""
    d = dict(usage_metadata or {})
    d["model"] = model
    d["source"] = "callback"
    return LLMCallUsage.from_dict(d)


def from_otel_span(attributes: dict[str, Any]) -> LLMCallUsage:
    """Any framework instrumented with OpenTelemetry GenAI (OpenInference / OpenLLMetry):
    CrewAI, Google ADK, LlamaIndex, Autogen, OpenAI Agents SDK … all emit these attrs."""
    d = dict(attributes or {})
    d["source"] = "otel"
    return LLMCallUsage.from_dict(d)


def from_provider_response(resp: Any, model: str = "") -> LLMCallUsage:
    """A raw OpenAI/Anthropic/LiteLLM response object with a ``.usage`` attribute."""
    u = getattr(resp, "usage", None) or {}
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    return LLMCallUsage(
        model=model or getattr(resp, "model", "") or "",
        input_tokens=int(get("input_tokens") or get("prompt_tokens") or 0),
        output_tokens=int(get("output_tokens") or get("completion_tokens") or 0),
        cache_read_tokens=int(get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(get("cache_creation_input_tokens") or 0),
        source="callback",
    )
