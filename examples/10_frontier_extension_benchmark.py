"""Frontier Extension Benchmark — extends 3-cohort coverage to GPT-5.5 + Gemini 3.1 Pro.

Runs **4 cohorts** that extend the original three-cohort benchmark
(`examples/09_three_cohort_benchmark.py`) to the OpenAI and Google frontier
families. Together with the prior Opus-as-agent results, this completes a 3x2
matrix:

                            Cost-floor harness     Frontier cross-family
  Anthropic agent (Opus)    [previous Cohort 2]    [previous Cohort 1]
  OpenAI agent (GPT-5.5)    B2 (this script)       A2 (this script)
  Google agent (Gemini)     B3 (this script)       A3 (this script)

  Cohort B2 - GPT-5.5 + cheap harness
    Pass 1 harness:  gemma-4-E4B-it-MLX-8bit (local proxy)
    Pass 2 harness:  anthropic/claude-haiku-4-5
    Agent:           gpt-5.5
    Out-of-pool:     OpenAI (no same-family juror)

  Cohort B3 - Gemini 3.1 Pro + cheap harness
    Pass 1 harness:  gpt-4.1-mini
    Pass 2 harness:  anthropic/claude-haiku-4-5
    Agent:           gemini/gemini-3.1-pro-preview
    Out-of-pool:     Google (no same-family juror)

  Cohort A2 - GPT-5.5 + cross-family frontier harness
    Pass 1 harness:  claude-opus-4-7
    Pass 2 harness:  gemini/gemini-3.1-pro-preview
    Agent:           gpt-5.5
    Out-of-pool:     OpenAI

  Cohort A3 - Gemini 3.1 Pro + cross-family frontier harness
    Pass 1 harness:  claude-opus-4-7
    Pass 2 harness:  gpt-5.5
    Agent:           gemini/gemini-3.1-pro-preview
    Out-of-pool:     Google

Each cohort runs **4 evals** (1 seed x 20 turns on pass 1, 3 seeds x 20 turns
on pass 2). 4 cohorts x 4 runs = **16 total runs**. Per-cohort outputs land in
their own subfolder for traceability:

    results/frontier_extension_<timestamp>/
      B2_gpt55_cost_floor/
        run_p1_seed42_turn20.json
        run_p2_seed{42,7,100}_turn20.json
        cohort_summary.json
        cohort_summary.md
      B3_gemini_cost_floor/   (same shape)
      A2_gpt55_frontier/      (same shape)
      A3_gemini_frontier/     (same shape)
      benchmark_summary.json  (cross-cohort comparison)
      benchmark_summary.md

Quickstart:
    # Default (all 4 cohorts; you only need the proxy for B2)
    python examples/10_frontier_extension_benchmark.py \\
        --proxy-url-pass1 http://localhost:1234/v1

    # Run specific cohorts (e.g., skip B2 if no local proxy)
    python examples/10_frontier_extension_benchmark.py --cohorts 2 3 4

    # Override seeds / turns
    python examples/10_frontier_extension_benchmark.py \\
        --proxy-url-pass1 http://localhost:1234/v1 \\
        --seeds-pass1 42 --seeds-pass2 42 7 100 --turns-pass1 20 --turns-pass2 20

Backup / fallback strategy (default ON):
    Each `run_one()` is wrapped in fallback retry: if the primary LLM fails
    with a transient unavailability error (connection / 5xx / overloaded), the
    runner re-runs the WHOLE eval with a same-tier fallback. Tiering is
    preserved so the cohort's intent isn't silently changed. Defined in
    MODEL_FALLBACK_CHAINS. Disable with --no-fallback for strict primary-only.

Required env:
    OPENAI_API_KEY      (gpt-5.5, gpt-4.1-mini, gpt-4o-mini)
    ANTHROPIC_API_KEY   (claude-opus-4-7, claude-haiku-4-5)
    GEMINI_API_KEY      (gemini/gemini-3.1-pro-preview via Gemini API)
                        OR set GOOGLE_APPLICATION_CREDENTIALS for Vertex AI
                        and use vertex_ai/gemini-3.1-pro instead
    LM Studio / proxy   (B2 pass-1 only; required when B2 is selected)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_GOLDEN_PATH = Path(__file__).resolve().parent / "08_golden_adversarial.py"
_spec = importlib.util.spec_from_file_location("golden", _GOLDEN_PATH)
assert _spec and _spec.loader
_golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_golden)

run_one = _golden.run_one
aggregate = _golden.aggregate
ship_decision = _golden.ship_decision
_wire_proxy_for_harness_llm = _golden._wire_proxy_for_harness_llm
make_agent = _golden.make_agent
CANONICAL_METRICS = _golden.CANONICAL_METRICS
SHIP_GATE = _golden.SHIP_GATE

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: ping every LLM before any eval starts (fail-fast)
# ─────────────────────────────────────────────────────────────────────────────

def _ping_litellm(model: str) -> str:
    """Tiny LLM call for pre-flight reachability check. Handles GPT-5.x /
    o-series reasoning models (which require `max_completion_tokens` instead
    of `max_tokens` and may reject `temperature`) and gives generous output
    budget so reasoning tokens don't crowd out visible output."""
    import litellm

    ml = model.lower()
    is_reasoning = any(s in ml for s in ("gpt-5", "o1-", "o3-", "o4-"))
    is_thinking = any(s in ml for s in ("gemini-2.5", "gemini-3", "claude-opus-4-7", "claude-opus-4-8"))

    base_kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "timeout": 30,
    }
    if is_reasoning:
        base_kwargs["max_completion_tokens"] = 256
    else:
        # Thinking models (Gemini 2.5+, Opus 4.7+) need more tokens because
        # internal reasoning consumes the budget before visible output.
        base_kwargs["max_tokens"] = 512 if is_thinking else 64
        if not is_thinking:
            base_kwargs["temperature"] = 0

    try:
        r = litellm.completion(**base_kwargs)
        return (r.choices[0].message.content or "").strip()
    except Exception as exc:
        err_msg = str(exc).lower()
        # Runtime fallback: rename max_tokens -> max_completion_tokens, drop temperature
        mutated = False
        if "max_tokens" in err_msg and "max_completion_tokens" in err_msg and "max_tokens" in base_kwargs:
            base_kwargs["max_completion_tokens"] = max(256, base_kwargs.pop("max_tokens", 64))
            mutated = True
        if (
            "temperature" in err_msg
            and any(s in err_msg for s in ("does not support", "unsupported", "deprecated", "not supported"))
            and "temperature" in base_kwargs
        ):
            base_kwargs.pop("temperature", None)
            mutated = True
        if not mutated:
            raise
        r = litellm.completion(**base_kwargs)
        return (r.choices[0].message.content or "").strip()


def _required_envs_for_models(models: list[str]) -> set[str]:
    """Map a list of model names to the env vars they require."""
    needed: set[str] = set()
    for m in models:
        ml = m.lower()
        if "claude" in ml or "anthropic" in ml:
            needed.add("ANTHROPIC_API_KEY")
        if "gemini" in ml:
            needed.add("GEMINI_API_KEY")
        if "gpt" in ml or "openai/" in ml or m == "gpt-4.1" or m.startswith("gpt-"):
            needed.add("OPENAI_API_KEY")
    return needed


def preflight_check_models(
    selected_cohorts: list[dict[str, Any]],
    proxy_url: str | None,
    console: Console,
) -> tuple[bool, list[str]]:
    """Ping every unique LLM in the benchmark plus every agent path.

    Catches: missing env vars, bad model names, unreachable providers,
    auth failures, agent-routing bugs, proxy misconfiguration. Aborts
    BEFORE any eval runs — saves hours when the harness LLM keys are wrong
    or a provider is down.
    """
    import os

    console.print()
    console.print(Rule("Pre-flight LLM check (all models)", style="bold yellow"))

    # Step 1: env-var presence check
    all_models: list[str] = []
    for c in selected_cohorts:
        all_models.extend([
            c["agent_model"],
            c["harness_llm_pass1"],
            c["harness_llm_pass2"],
        ])
    needed_envs = _required_envs_for_models(all_models)
    env_errors: list[str] = []
    for env in sorted(needed_envs):
        if not os.environ.get(env):
            env_errors.append(f"  Missing env var: {env}")
    if env_errors:
        console.print()
        console.print("[bold red]Environment variable errors:[/bold red]")
        for e in env_errors:
            console.print(f"[red]{e}[/red]")
        console.print()
        console.print(Panel(
            "\n".join(env_errors)
            + "\n\nSet the missing env vars in your shell, then re-run.",
            title="[red]Pre-flight FAILED — aborting before any eval runs[/red]",
            border_style="red",
        ))
        return False, env_errors

    # Step 2: collect unique (model, role, is_proxied) combinations
    checks: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for c in selected_cohorts:
        agent_key = (c["agent_model"], False)
        if agent_key not in seen:
            seen.add(agent_key)
            checks.append((c["agent_model"], "agent", False))
        p1_key = (c["harness_llm_pass1"], c["uses_proxy_pass1"])
        if p1_key not in seen:
            seen.add(p1_key)
            checks.append((c["harness_llm_pass1"], "harness P1", c["uses_proxy_pass1"]))
        p2_key = (c["harness_llm_pass2"], False)
        if p2_key not in seen:
            seen.add(p2_key)
            checks.append((c["harness_llm_pass2"], "harness P2", False))

    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")

    console.print(
        f"\n[dim]Pinging {len(checks)} unique model/role combinations "
        f"(short prompt, ~30s timeout each)[/dim]\n"
    )

    errors: list[str] = []
    for model, role, is_proxied in checks:
        target_model = model
        display_model = model
        if is_proxied:
            if not proxy_url:
                err = f"  {model} ({role}): proxy URL required but not provided"
                errors.append(err)
                console.print(f"[red]  ✗ {model} ({role}): proxy URL missing[/red]")
                continue
            target_model = _wire_proxy_for_harness_llm(proxy_url, model)
            display_model = f"{model} (via proxy)"

        try:
            content = _ping_litellm(target_model)
            if not content:
                err = f"  {target_model} ({role}): empty response"
                errors.append(err)
                console.print(f"[red]  ✗ {display_model} ({role}): empty response[/red]")
            else:
                console.print(f"[green]  ✓ {display_model} ({role}): {content[:30]!r}[/green]")
        except Exception as exc:
            short_err = str(exc)[:200].replace("\n", " ")
            err = f"  {target_model} ({role}): {type(exc).__name__}: {short_err}"
            errors.append(err)
            console.print(f"[red]  ✗ {display_model} ({role}): {type(exc).__name__}: {short_err[:120]}[/red]")
        finally:
            if is_proxied:
                _restore_proxy_env(saved_base, saved_key)

    # Step 3: agent-path check (catches make_agent() routing bugs)
    console.print()
    console.print("[dim]Verifying agent constructor for each agent model...[/dim]")
    unique_agents = {c["agent_model"] for c in selected_cohorts}
    for agent_model in sorted(unique_agents):
        try:
            agent = make_agent(model=agent_model)
            r = agent("Reply with just: OK")
            text = (r.text or "").strip()
            if not text:
                err = f"  agent({agent_model}): empty response"
                errors.append(err)
                console.print(f"[red]  ✗ agent({agent_model}): empty response[/red]")
            else:
                console.print(f"[green]  ✓ agent({agent_model}): {text[:30]!r}[/green]")
        except Exception as exc:
            short_err = str(exc)[:200].replace("\n", " ")
            err = f"  agent({agent_model}): {type(exc).__name__}: {short_err}"
            errors.append(err)
            console.print(f"[red]  ✗ agent({agent_model}): {type(exc).__name__}: {short_err[:120]}[/red]")

    if errors:
        console.print()
        console.print(Panel(
            "\n".join(errors[:15])
            + (f"\n  ... and {len(errors) - 15} more" if len(errors) > 15 else ""),
            title="[red]Pre-flight FAILED — aborting before any eval runs[/red]",
            border_style="red",
        ))
        return False, errors

    console.print()
    console.print("[bold green]✓ All pre-flight checks passed — proceeding with benchmark[/bold green]")
    return True, []


# ─────────────────────────────────────────────────────────────────────────────
# Backup / fallback strategy
# ─────────────────────────────────────────────────────────────────────────────

# Fallback chains by capability tier. Lookup strips known provider prefixes
# (`openai/`, `anthropic/`, `gemini/`, `vertex_ai/`) so a primary name with or
# without prefix resolves to the same chain.
MODEL_FALLBACK_CHAINS: dict[str, list[str]] = {
    # === Local cheap-floor (proxied) ===
    "gemma-4-E4B-it-MLX-8bit": ["gpt-4.1-mini", "gpt-4o-mini"],

    # === Cheap API tier ===
    "gpt-4o-mini": ["gpt-4.1-mini", "anthropic/claude-haiku-4-5"],
    "gpt-4.1-mini": ["gpt-4o-mini", "anthropic/claude-haiku-4-5"],
    "claude-haiku-4-5": ["gpt-4o-mini", "gpt-4.1-mini"],

    # === Frontier general-purpose tier ===
    "gpt-5.5": ["gpt-5.4", "gpt-4.1"],
    "gpt-4.1": ["anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4-7"],
    "claude-sonnet-4-6": ["gpt-4.1", "anthropic/claude-opus-4-7"],
    "claude-opus-4-7": ["anthropic/claude-sonnet-4-6", "gpt-4.1"],
    "gemini-3.1-pro-preview": ["gemini/gemini-3.0-pro", "gemini/gemini-2.5-pro"],
    "gemini-3.1-pro": ["gemini/gemini-3.1-pro-preview", "gemini/gemini-2.5-pro"],
    "gemini-2.5-pro": ["gemini/gemini-2.0-flash", "anthropic/claude-sonnet-4-6"],
}

_TRANSIENT_ERROR_SUBSTRINGS = (
    "connection error", "connection refused", "connection reset",
    "service unavailable", "internal server error", "bad gateway",
    "gateway timeout", "read timeout", "request timeout",
    "overloaded", "model_overloaded", "model overloaded",
    " 503", " 502", " 504",
    "litellm.serviceunavailableerror", "litellm.apiconnectionerror",
    "litellm.internalservererror", "litellm.timeout",
    "remote end closed connection", "max retries exceeded",
    "name or service not known",
    "ssl",
    "no route to host",
)

_PROVIDER_PREFIXES = ("openai/", "anthropic/", "gemini/", "vertex_ai/")


def _is_transient(error_str: str) -> bool:
    msg = error_str.lower()
    return any(s in msg for s in _TRANSIENT_ERROR_SUBSTRINGS)


def _resolve_fallback_chain(primary: str) -> list[str]:
    """Look up a fallback chain by primary model name, stripping any known
    provider prefix so `gemini/gemini-3.1-pro-preview` resolves to the same entry as
    `gemini-3.1-pro`."""
    base = primary
    for prefix in _PROVIDER_PREFIXES:
        base = base.removeprefix(prefix)
    return MODEL_FALLBACK_CHAINS.get(base) or MODEL_FALLBACK_CHAINS.get(primary, [])


def _restore_proxy_env(saved_base: str | None, saved_key: str | None) -> None:
    """Restore the OpenAI env vars that were live before proxy wiring."""
    import os
    if saved_base is None:
        os.environ.pop("OPENAI_BASE_URL", None)
    else:
        os.environ["OPENAI_BASE_URL"] = saved_base
    if saved_key is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = saved_key


def run_one_with_fallback(
    *,
    primary_harness_llm: str,
    enable_fallback: bool,
    is_proxied: bool,
    saved_openai_base: str | None,
    saved_openai_key: str | None,
    console: Console,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Run one eval; on transient harness LLM failure, swap to fallback chain."""
    chain = _resolve_fallback_chain(primary_harness_llm) if enable_fallback else []
    candidates = [primary_harness_llm, *chain]
    last_result: dict[str, Any] = {}

    for i, candidate in enumerate(candidates):
        is_fallback = i > 0
        if is_fallback and is_proxied:
            _restore_proxy_env(saved_openai_base, saved_openai_key)

        result = run_one(
            harness_llm_model=candidate,
            console=console,
            **run_kwargs,
        )

        if "error" not in result:
            if is_fallback:
                console.print(
                    f"  [yellow]↪ Fallback succeeded:[/yellow] "
                    f"[dim]{primary_harness_llm}[/dim] → [bold]{candidate}[/bold]"
                )
                result["fallback_used"] = True
                result["original_harness_llm"] = primary_harness_llm
                result["actual_harness_llm"] = candidate
            return result

        last_result = result
        err = result["error"]

        if not _is_transient(err):
            console.print(
                f"  [red]✗ Non-transient error — not falling back:[/red] "
                f"{err[:100]}"
            )
            return result

        next_candidate = candidates[i + 1] if i + 1 < len(candidates) else None
        if next_candidate:
            label = "Primary" if i == 0 else f"Fallback {candidate}"
            console.print(
                f"  [yellow]⚠ {label} transient failure:[/yellow] {err[:100]}"
            )
            console.print(
                f"  [yellow]↻ Trying next:[/yellow] [bold]{next_candidate}[/bold]"
            )
        else:
            console.print(
                f"  [red]✗ All fallbacks exhausted for {primary_harness_llm}[/red]"
            )

    if last_result:
        last_result["fallback_used"] = True
        last_result["original_harness_llm"] = primary_harness_llm
        last_result["actual_harness_llm"] = "ALL_FAILED"
    return last_result


# ─────────────────────────────────────────────────────────────────────────────
# Cohort definitions (4 cohorts: B2, B3, A2, A3)
# ─────────────────────────────────────────────────────────────────────────────

COHORTS: list[dict[str, Any]] = [
    {
        "id": "B2_gpt55_cost_floor",
        "name": "B2 — GPT-5.5 + Cost-Floor Harness",
        "harness_llm_pass1": "gemma-4-E4B-it-MLX-8bit",
        "harness_llm_pass2": "anthropic/claude-haiku-4-5",
        "agent_model": "gpt-5.5",
        "uses_proxy_pass1": True,
        "description": (
            "Cost-floor harness (local Gemma + Anthropic Haiku) testing OpenAI's "
            "GPT-5.5 frontier agent. Replicates the cost-floor calibration gap "
            "experiment for the OpenAI family. Out-of-pool: OpenAI."
        ),
        "predicted_verdict": (
            "GOLD/SILVER (mean ~9.0-9.7) IF cost-floor over-rating reproduces; "
            "rubber-stamping signature expected"
        ),
    },
    {
        "id": "B3_gemini_cost_floor",
        "name": "B3 — Gemini 3.1 Pro + Cost-Floor Harness",
        "harness_llm_pass1": "gpt-4.1-mini",
        "harness_llm_pass2": "anthropic/claude-haiku-4-5",
        "agent_model": "gemini/gemini-3.1-pro-preview",
        "uses_proxy_pass1": False,
        "description": (
            "Cost-floor harness (cheap APIs from OpenAI + Anthropic) testing "
            "Google's Gemini 3.1 Pro frontier agent. Replicates the cost-floor "
            "calibration gap experiment for the Google family. Out-of-pool: Google."
        ),
        "predicted_verdict": (
            "GOLD/SILVER (mean ~8.5-9.5) IF cost-floor over-rating reproduces "
            "across the Google family"
        ),
    },
    {
        "id": "A2_gpt55_frontier",
        "name": "A2 — GPT-5.5 + Cross-Family Frontier Harness",
        "harness_llm_pass1": "claude-opus-4-7",
        "harness_llm_pass2": "gemini/gemini-2.5-pro",
        "agent_model": "gpt-5.5",
        "uses_proxy_pass1": False,
        "description": (
            "Cross-family frontier harness (Opus + Gemini) testing OpenAI's "
            "GPT-5.5 with no same-family juror. Tests whether GPT-5.5 hits "
            "SILVER+ where Opus failed at 7.15 in the original benchmark. "
            "Out-of-pool: OpenAI."
        ),
        "predicted_verdict": "NEEDS_ENH (mean ~6.5-7.8); may match Opus baseline",
    },
    {
        "id": "A3_gemini_frontier",
        "name": "A3 — Gemini 2.5 Pro + Cross-Family Frontier Harness",
        "harness_llm_pass1": "claude-opus-4-7",
        "harness_llm_pass2": "gpt-5.5",
        "agent_model": "gemini/gemini-2.5-pro",
        "uses_proxy_pass1": False,
        "description": (
            "Cross-family frontier harness (Opus + GPT-5.5) testing Google's "
            "Gemini 2.5 Pro stable (3.1-pro-preview unavailable for this run) "
            "with no same-family juror. Out-of-pool: Google."
        ),
        "predicted_verdict": "NEEDS_ENH (mean ~6.5-7.8); may match Opus baseline",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Per-cohort runner
# ─────────────────────────────────────────────────────────────────────────────

def run_cohort(
    *,
    cohort: dict[str, Any],
    cohort_dir: Path,
    seeds_pass1: list[int],
    seeds_pass2: list[int],
    turns_pass1: int,
    turns_pass2: int,
    consensus: str,
    proxy_url: str | None,
    context_budget_pass1: int | None,
    verbose: bool = True,
    console: Console,
    enable_fallback: bool = True,
) -> dict[str, Any]:
    """Run one cohort (pass 1 + pass 2). Returns aggregated cohort dict."""
    import os

    cohort_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")

    def _restore_env() -> None:
        _restore_proxy_env(saved_base, saved_key)

    total_evals = len(seeds_pass1) + len(seeds_pass2)

    def _eval_separator(eval_idx: int, label: str, seed: int, turns: int, model: str) -> None:
        console.print()
        console.print(
            Rule(
                f"  EVAL {eval_idx}/{total_evals} — {label} | seed={seed} "
                f"| turns={turns} | model={model}  ",
                style="bold blue",
            )
        )

    # ── Pass 1 ─────────────────────────────────────────────────────────
    harness_llm_pass1 = cohort["harness_llm_pass1"]
    if cohort["uses_proxy_pass1"]:
        if not proxy_url:
            raise SystemExit(
                f"Cohort '{cohort['name']}' requires --proxy-url-pass1 "
                f"(harness LLM pass 1 = {harness_llm_pass1})."
            )
        harness_llm_pass1 = _wire_proxy_for_harness_llm(proxy_url, cohort["harness_llm_pass1"])
        console.print(
            f"[dim][config] Pass 1 proxy: {proxy_url} → "
            f"harness LLM={harness_llm_pass1}[/dim]"
        )

    console.print(
        Rule(
            f"  Pass 1 — {harness_llm_pass1} @ {turns_pass1} turns  ",
            style="cyan",
        )
    )
    for i, seed in enumerate(seeds_pass1):
        stem = f"run_p1_seed{seed}_turn{turns_pass1}"
        _eval_separator(i + 1, "Pass 1", seed, turns_pass1, harness_llm_pass1)
        results.append(run_one_with_fallback(
            primary_harness_llm=harness_llm_pass1,
            enable_fallback=enable_fallback,
            is_proxied=cohort["uses_proxy_pass1"],
            saved_openai_base=saved_base,
            saved_openai_key=saved_key,
            console=console,
            agent_model=cohort["agent_model"],
            turns=turns_pass1,
            seed=seed,
            consensus=consensus,
            pass_label="P1",
            verbose=verbose,
            context_budget_tokens=context_budget_pass1,
            output_dir=cohort_dir,
            output_stem=stem,
        ))

    _restore_env()

    # ── Pass 2 ─────────────────────────────────────────────────────────
    harness_llm_pass2 = cohort["harness_llm_pass2"]
    console.print(
        Rule(
            f"  Pass 2 — {harness_llm_pass2} @ {turns_pass2} turns  ",
            style="cyan",
        )
    )
    for j, seed in enumerate(seeds_pass2):
        stem = f"run_p2_seed{seed}_turn{turns_pass2}"
        _eval_separator(
            len(seeds_pass1) + j + 1, "Pass 2", seed, turns_pass2, harness_llm_pass2,
        )
        results.append(run_one_with_fallback(
            primary_harness_llm=harness_llm_pass2,
            enable_fallback=enable_fallback,
            is_proxied=False,
            saved_openai_base=saved_base,
            saved_openai_key=saved_key,
            console=console,
            agent_model=cohort["agent_model"],
            turns=turns_pass2,
            seed=seed,
            consensus=consensus,
            pass_label="P2",
            verbose=verbose,
            output_dir=cohort_dir,
            output_stem=stem,
        ))

    _restore_env()

    agg = aggregate(results)
    agg["cohort_id"] = cohort["id"]
    agg["cohort_name"] = cohort["name"]
    agg["agent_model"] = cohort["agent_model"]
    agg["harness_llm_pass1"] = harness_llm_pass1
    agg["harness_llm_pass2"] = harness_llm_pass2

    fallback_events = [
        {
            "pass": r["pass"],
            "seed": r["seed"],
            "original": r["original_harness_llm"],
            "actual": r["actual_harness_llm"],
        }
        for r in agg["raw"] if r.get("fallback_used")
    ] + [
        {
            "pass": r["pass"],
            "seed": r["seed"],
            "original": r.get("original_harness_llm", r.get("harness_llm")),
            "actual": "ALL_FAILED",
        }
        for r in results if r.get("fallback_used") and "error" in r
    ]
    agg["fallback_events"] = fallback_events

    summary_json = {
        "cohort_id": cohort["id"],
        "cohort_name": cohort["name"],
        "description": cohort["description"],
        "agent_model": cohort["agent_model"],
        "harness_llm_pass1": harness_llm_pass1,
        "harness_llm_pass2": harness_llm_pass2,
        "turns_pass1": turns_pass1,
        "turns_pass2": turns_pass2,
        "seeds_pass1": seeds_pass1,
        "seeds_pass2": seeds_pass2,
        "consensus": consensus,
        "n_runs": agg["n_runs"],
        "n_ok": agg["n_ok"],
        "n_failed": agg["n_failed"],
        "overall": agg["overall"],
        "per_metric": agg["per_metric"],
        "persistent_low": agg["persistent_low"],
        "cert_tally": agg["cert_tally"],
        "warnings": {
            "plateau": agg["plateau_count"],
            "dissent": agg["dissent_count"],
            "ceiling": agg["ceiling_count"],
        },
        "ship_decision": dict(zip(
            ["verdict", "passes", "fails"],
            ship_decision(agg),
            strict=False,
        )),
        "fallback_events": fallback_events,
        "raw_runs": agg["raw"],
    }
    (cohort_dir / "cohort_summary.json").write_text(
        json.dumps(summary_json, indent=2, default=str)
    )

    md_lines = [
        f"# Cohort: {cohort['name']}",
        "",
        f"**ID:** `{cohort['id']}`",
        f"**Agent:** `{cohort['agent_model']}`",
        f"**Pass 1 harness LLM:** `{harness_llm_pass1}` ({turns_pass1} turns x {len(seeds_pass1)} seeds)",
        f"**Pass 2 harness LLM:** `{harness_llm_pass2}` ({turns_pass2} turns x {len(seeds_pass2)} seeds)",
        f"**Consensus:** `{consensus}`",
        "",
        f"**Description:** {cohort['description']}",
        "",
        "## Per-run results",
        "",
        "| # | Pass | Seed | Turns | Score | Cert | Tokens | Duration |",
        "|---|------|-----:|------:|------:|------|-------:|---------:|",
    ]
    for i, r in enumerate(agg["raw"], 1):
        if "error" in r:
            md_lines.append(
                f"| {i} | {r['pass']} | {r['seed']} | {r['turns']} | "
                f"ERROR | — | — | {r['duration']:.0f}s |"
            )
        else:
            md_lines.append(
                f"| {i} | {r['pass']} | {r['seed']} | {r['turns']} | "
                f"{r['final_score']:.2f} | {r['certification']} | "
                f"{r['tokens']:,} | {r['duration']:.0f}s |"
            )
    md_lines.extend([
        "",
        "## Cohort statistics (final score)",
        "",
        f"- **Mean:** {agg['overall']['mean']:.2f}",
        f"- **Median:** {agg['overall']['median']:.2f}",
        f"- **Min:** {agg['overall']['min']:.2f}",
        f"- **Max:** {agg['overall']['max']:.2f}",
        f"- **Spread:** {agg['overall']['spread']:.2f}",
        "",
        "## Per-metric across all runs",
        "",
        "| Metric | Min | Med | Mean | Max | Spread |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for m, s in agg["per_metric"].items():
        md_lines.append(
            f"| {m} | {s['min']:.1f} | {s['median']:.1f} | "
            f"{s['mean']:.2f} | {s['max']:.1f} | {s['spread']:.2f} |"
        )
    md_lines.extend([
        "",
        f"## Cert tally: {dict(agg['cert_tally'])}",
        "",
        f"## Persistent low metrics: {agg['persistent_low'] or '(none)'}",
        "",
    ])
    if fallback_events:
        md_lines.extend([
            "## Fallback events",
            "",
            "Primary harness LLM was unresponsive on these runs; the runner "
            "swapped to a same-tier fallback model so the cohort completed.",
            "",
            "| Pass | Seed | Original | Actual |",
            "|------|-----:|----------|--------|",
        ])
        for ev in fallback_events:
            md_lines.append(
                f"| {ev['pass']} | {ev['seed']} | `{ev['original']}` | "
                f"`{ev['actual']}` |"
            )
        md_lines.append("")
    (cohort_dir / "cohort_summary.md").write_text("\n".join(md_lines))

    return agg


# ─────────────────────────────────────────────────────────────────────────────
# Cross-cohort comparison
# ─────────────────────────────────────────────────────────────────────────────

def render_per_cohort_table(
    cohort_aggs: list[dict[str, Any]], console: Console
) -> None:
    table = Table(
        title="Per-cohort summary",
        title_style="bold cyan",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Cohort", style="bold")
    table.add_column("Agent")
    table.add_column("Harness LLM (P1 + P2)")
    table.add_column("Mean", justify="right")
    table.add_column("Median", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Spread", justify="right")
    table.add_column("Cert tally")

    for i, agg in enumerate(cohort_aggs, 1):
        if agg["n_ok"] == 0:
            table.add_row(
                str(i),
                agg["cohort_name"],
                agg["agent_model"][:22],
                f"{agg['harness_llm_pass1'][:18]} + {agg['harness_llm_pass2'][:18]}",
                Text("ALL FAIL", style="bold red"),
                "—", "—", "—", "—",
                "—",
            )
            continue
        o = agg["overall"]
        cert_str = ", ".join(f"{c}x{n}" for c, n in agg["cert_tally"].items())
        spread_color = (
            "green" if o["spread"] < 0.5
            else "yellow" if o["spread"] < 1.0
            else "red"
        )
        table.add_row(
            str(i),
            agg["cohort_name"],
            agg["agent_model"][:22],
            f"{agg['harness_llm_pass1'][:18]} + {agg['harness_llm_pass2'][:18]}",
            f"{o['mean']:.2f}",
            f"{o['median']:.2f}",
            f"{o['min']:.2f}",
            f"{o['max']:.2f}",
            Text(f"{o['spread']:.2f}", style=spread_color),
            cert_str,
        )

    console.print()
    console.print(table)

    fb_lines: list[str] = []
    for agg in cohort_aggs:
        for ev in agg.get("fallback_events", []):
            fb_lines.append(
                f"  [yellow]↪[/yellow] {agg['cohort_name']} {ev['pass']} "
                f"seed={ev['seed']}: {ev['original']} → [bold]{ev['actual']}[/bold]"
            )
    if fb_lines:
        console.print()
        console.print("[bold yellow]Fallback events:[/bold yellow]")
        for line in fb_lines:
            console.print(line)


def render_cross_cohort_comparison(
    cohort_aggs: list[dict[str, Any]], console: Console
) -> dict[str, Any]:
    """Render the headline cross-cohort comparisons.

    Four meaningful pairwise deltas for the 4-cohort design:
      - B2 vs A2: same OpenAI agent, cost-floor vs frontier harness
      - B3 vs A3: same Gemini agent, cost-floor vs frontier harness
      - A2 vs A3: same harness tier (frontier), OpenAI vs Gemini agent
      - B2 vs B3: same harness tier (cost-floor), OpenAI vs Gemini agent
    """
    by_id = {a["cohort_id"]: a for a in cohort_aggs}
    b2 = by_id.get("B2_gpt55_cost_floor")
    b3 = by_id.get("B3_gemini_cost_floor")
    a2 = by_id.get("A2_gpt55_frontier")
    a3 = by_id.get("A3_gemini_frontier")

    comparisons: dict[str, Any] = {}
    table = Table(
        title="Cross-cohort comparisons",
        title_style="bold cyan",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Comparison", style="bold")
    table.add_column("Δ Mean", justify="right")
    table.add_column("Cert agreement")
    table.add_column("Interpretation", style="dim")

    def _mean(a: dict[str, Any] | None) -> float | None:
        if a is None or a["n_ok"] == 0:
            return None
        return a["overall"]["mean"]

    def _cert(a: dict[str, Any] | None) -> str:
        if a is None:
            return "—"
        return max(a["cert_tally"], key=a["cert_tally"].get) if a["cert_tally"] else "—"

    if b2 and a2 and _mean(b2) is not None and _mean(a2) is not None:
        delta = _mean(b2) - _mean(a2)
        same_cert = _cert(b2) == _cert(a2)
        interp = (
            "✓ Cost-floor preserves verdict on GPT-5.5" if same_cert
            else f"⚠ Cost-floor over-rates GPT-5.5 by {delta:+.2f} pts (cert diverged)"
        )
        table.add_row(
            "B2 vs A2 — GPT-5.5 agent: cheap vs frontier harness",
            f"{delta:+.2f}",
            "same" if same_cert else "differ",
            interp,
        )
        comparisons["b2_vs_a2"] = {
            "delta_mean": delta, "same_cert": same_cert,
            "b2_cert": _cert(b2), "a2_cert": _cert(a2),
        }

    if b3 and a3 and _mean(b3) is not None and _mean(a3) is not None:
        delta = _mean(b3) - _mean(a3)
        same_cert = _cert(b3) == _cert(a3)
        interp = (
            "✓ Cost-floor preserves verdict on Gemini" if same_cert
            else f"⚠ Cost-floor over-rates Gemini by {delta:+.2f} pts (cert diverged)"
        )
        table.add_row(
            "B3 vs A3 — Gemini agent: cheap vs frontier harness",
            f"{delta:+.2f}",
            "same" if same_cert else "differ",
            interp,
        )
        comparisons["b3_vs_a3"] = {
            "delta_mean": delta, "same_cert": same_cert,
            "b3_cert": _cert(b3), "a3_cert": _cert(a3),
        }

    if a2 and a3 and _mean(a2) is not None and _mean(a3) is not None:
        delta = _mean(a2) - _mean(a3)
        diff_cert = _cert(a2) != _cert(a3)
        interp = (
            f"Frontier-vs-frontier (Δ {abs(delta):.2f} pts): "
            f"{'OpenAI > Gemini' if delta > 0 else 'Gemini > OpenAI' if delta < 0 else 'tied'}"
        )
        table.add_row(
            "A2 vs A3 — frontier harness: OpenAI vs Gemini agent",
            f"{delta:+.2f}",
            "differ" if diff_cert else "same",
            interp,
        )
        comparisons["a2_vs_a3"] = {
            "delta_mean": delta, "diff_cert": diff_cert,
            "a2_cert": _cert(a2), "a3_cert": _cert(a3),
        }

    if b2 and b3 and _mean(b2) is not None and _mean(b3) is not None:
        delta = _mean(b2) - _mean(b3)
        diff_cert = _cert(b2) != _cert(b3)
        interp = (
            f"Cheap-harness consistency check (Δ {abs(delta):.2f} pts): "
            f"{'OpenAI rubber-stamped > Gemini' if delta > 0 else 'Gemini rubber-stamped > OpenAI' if delta < 0 else 'tied'}"
        )
        table.add_row(
            "B2 vs B3 — cheap harness: OpenAI vs Gemini agent",
            f"{delta:+.2f}",
            "differ" if diff_cert else "same",
            interp,
        )
        comparisons["b2_vs_b3"] = {
            "delta_mean": delta, "diff_cert": diff_cert,
            "b2_cert": _cert(b2), "b3_cert": _cert(b3),
        }

    console.print()
    console.print(table)

    return comparisons


def write_benchmark_summary(
    benchmark_dir: Path,
    cohort_aggs: list[dict[str, Any]],
    comparisons: dict[str, Any],
    cohorts: list[dict[str, Any]],
    *,
    seeds_pass1: list[int],
    seeds_pass2: list[int],
    turns_pass1: int,
    turns_pass2: int,
    consensus: str,
    cohort_runtime_s: float,
) -> None:
    """Write benchmark_summary.json + benchmark_summary.md."""
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cohort_runtime_seconds": cohort_runtime_s,
        "config": {
            "seeds_pass1": seeds_pass1,
            "seeds_pass2": seeds_pass2,
            "turns_pass1": turns_pass1,
            "turns_pass2": turns_pass2,
            "consensus": consensus,
        },
        "cohorts": [
            {
                "id": agg["cohort_id"],
                "name": agg["cohort_name"],
                "agent_model": agg["agent_model"],
                "harness_llm_pass1": agg["harness_llm_pass1"],
                "harness_llm_pass2": agg["harness_llm_pass2"],
                "n_ok": agg["n_ok"],
                "n_failed": agg["n_failed"],
                "overall": agg["overall"],
                "per_metric": agg["per_metric"],
                "cert_tally": dict(agg["cert_tally"]),
                "persistent_low": agg["persistent_low"],
                "fallback_events": agg.get("fallback_events", []),
            }
            for agg in cohort_aggs
        ],
        "comparisons": comparisons,
    }
    (benchmark_dir / "benchmark_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    md = ["# Frontier Extension Benchmark — Summary", "", f"Generated: {summary['generated_at']}"]
    md.append("")
    md.append("## Cohort x Eval Configuration")
    md.append("")
    md.append(
        "| # | Cohort | Agent | P1 model | P1 seeds | P1 fallback | "
        "P2 model | P2 seeds | P2 fallback | Runs |"
    )
    md.append(
        "|---|--------|-------|----------|----------|-------------|"
        "----------|----------|-------------|-----:|"
    )
    p1_seeds_md = " ".join(str(s) for s in seeds_pass1)
    p2_seeds_md = " ".join(str(s) for s in seeds_pass2)
    runs_per_cohort = len(seeds_pass1) + len(seeds_pass2)

    def _fmt_fallback_md(model: str) -> str:
        chain = _resolve_fallback_chain(model)
        if not chain:
            return "(none)"
        return " → ".join(f"`{c}`" for c in chain)

    for i, c in enumerate(cohorts, 1):
        proxy_note = " *(proxy)*" if c["uses_proxy_pass1"] else ""
        md.append(
            f"| {i} | **{c['name']}**<br/>`{c['id']}` | `{c['agent_model']}` | "
            f"`{c['harness_llm_pass1']}`{proxy_note} | "
            f"{p1_seeds_md} x {turns_pass1}t | {_fmt_fallback_md(c['harness_llm_pass1'])} | "
            f"`{c['harness_llm_pass2']}` | "
            f"{p2_seeds_md} x {turns_pass2}t | {_fmt_fallback_md(c['harness_llm_pass2'])} | "
            f"{runs_per_cohort} |"
        )
    md.append("")
    md.append(
        f"**Total:** {runs_per_cohort} runs/cohort x {len(cohorts)} cohorts = "
        f"**{runs_per_cohort * len(cohorts)} total runs** "
        f"(consensus: `{consensus}`)."
    )
    md.append("")

    md.append("## Per-cohort results")
    md.append("")
    md.append("| # | Cohort | Mean | Median | Min | Max | Spread | Cert tally |")
    md.append("|---|--------|-----:|-------:|----:|----:|-------:|-----------|")
    for i, agg in enumerate(cohort_aggs, 1):
        if agg["n_ok"] == 0:
            md.append(f"| {i} | {agg['cohort_name']} | ALL FAIL | — | — | — | — | — |")
            continue
        o = agg["overall"]
        cert_str = ", ".join(f"{c}x{n}" for c, n in agg["cert_tally"].items())
        md.append(
            f"| {i} | {agg['cohort_name']} | {o['mean']:.2f} | {o['median']:.2f} | "
            f"{o['min']:.2f} | {o['max']:.2f} | {o['spread']:.2f} | {cert_str} |"
        )
    md.append("")

    md.append("## Cross-cohort comparisons")
    md.append("")
    for key, c in comparisons.items():
        if key == "b2_vs_a2":
            md.append(
                f"- **B2 → A2 (GPT-5.5 cost-floor delta):** Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['b2_cert']} vs {c['a2_cert']} "
                f"({'preserved ✓' if c['same_cert'] else 'diverged ⚠'})"
            )
        elif key == "b3_vs_a3":
            md.append(
                f"- **B3 → A3 (Gemini cost-floor delta):** Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['b3_cert']} vs {c['a3_cert']} "
                f"({'preserved ✓' if c['same_cert'] else 'diverged ⚠'})"
            )
        elif key == "a2_vs_a3":
            md.append(
                f"- **A2 → A3 (frontier-vs-frontier, OpenAI vs Gemini agent):** "
                f"Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['a2_cert']} vs {c['a3_cert']}"
            )
        elif key == "b2_vs_b3":
            md.append(
                f"- **B2 → B3 (cheap-harness consistency, OpenAI vs Gemini agent):** "
                f"Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['b2_cert']} vs {c['b3_cert']}"
            )
    md.append("")

    all_fallbacks = [
        (agg["cohort_name"], ev)
        for agg in cohort_aggs
        for ev in agg.get("fallback_events", [])
    ]
    if all_fallbacks:
        md.append("## Fallback events")
        md.append("")
        md.append(
            "Primary harness LLM was unresponsive on these runs; the runner "
            "swapped to a same-tier model so the cohort completed. Treat any "
            "scores from these runs with the caveat that the actual model "
            "differs from the cohort's nominal config."
        )
        md.append("")
        md.append("| Cohort | Pass | Seed | Original | Actual |")
        md.append("|--------|------|-----:|----------|--------|")
        for cname, ev in all_fallbacks:
            md.append(
                f"| {cname} | {ev['pass']} | {ev['seed']} | "
                f"`{ev['original']}` | `{ev['actual']}` |"
            )
        md.append("")
    else:
        md.append("## Fallback events")
        md.append("")
        md.append("None. All runs completed on their primary harness LLM.")
        md.append("")

    md.append(f"Total runtime: {cohort_runtime_s/60:.1f} min")
    md.append("")
    (benchmark_dir / "benchmark_summary.md").write_text("\n".join(md))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Frontier Extension Benchmark — extends 3-cohort coverage to "
            "GPT-5.5 + Gemini 3.1 Pro. 4 cohorts x 4 runs = 16 runs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--cohorts",
        type=int,
        nargs="+",
        choices=[1, 2, 3, 4],
        default=[1, 2, 3, 4],
        help="Which cohorts to run (1=B2, 2=B3, 3=A2, 4=A3). Default: all 4.",
    )
    p.add_argument(
        "--seeds-pass1", type=int, nargs="+", default=[42],
        help="Seeds for pass 1 within each cohort. Default: 42 (single-seed pass 1)",
    )
    p.add_argument(
        "--seeds-pass2", type=int, nargs="+", default=[42, 7, 100],
        help="Seeds for pass 2 within each cohort. Default: 42 7 100",
    )
    p.add_argument(
        "--turns-pass1", type=int, default=20,
        help="Turns per pass-1 run. Default: 20",
    )
    p.add_argument(
        "--turns-pass2", type=int, default=20,
        help="Turns per pass-2 run. Default: 20",
    )
    p.add_argument(
        "--consensus", default="debate",
        choices=["independent", "delphi", "debate"],
        help="Consensus strategy for all cohorts. Default: debate",
    )
    p.add_argument(
        "--proxy-url-pass1", default=None,
        help="OpenAI-compatible proxy URL for B2's pass-1 harness LLM "
             "(local Gemma). Required when running B2.",
    )
    p.add_argument(
        "--context-budget-pass1", type=int, default=None,
        help="Max prompt tokens for pass-1 harness LLM calls. Required when "
             "B2's local proxy has small loaded context (e.g., 6000 for "
             "8K-loaded Gemma).",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-pass live progress bar + scorecard.",
    )
    p.add_argument(
        "--output-tag", default=None,
        help="Optional suffix for the output folder (defaults to timestamp).",
    )
    p.add_argument(
        "--no-fallback", action="store_true",
        help="Disable model-fallback retry on transient unavailability.",
    )
    p.add_argument(
        "--skip-preflight", action="store_true",
        help="Skip the pre-flight LLM check that pings every model before "
             "running. Default: pre-flight runs. Skip ONLY if you've already "
             "validated all models in a recent run.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    console = Console()

    tag = args.output_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    benchmark_dir = RESULTS_DIR / f"frontier_extension_{tag}"
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    selected_cohorts = [COHORTS[i - 1] for i in args.cohorts]

    console.print(Rule("Frontier Extension Benchmark (4 cohorts)", style="bold magenta"))
    fallback_status = (
        "[red]disabled[/red]" if args.no_fallback
        else "[green]enabled[/green] (transient failures swap to same-tier model)"
    )
    console.print(
        f"\n[bold]Output folder:[/bold] {benchmark_dir.relative_to(Path.cwd())}\n"
        f"[bold]Cohorts to run:[/bold] {len(selected_cohorts)} of 4\n"
        f"[bold]Pass 1:[/bold] {len(args.seeds_pass1)} seed(s) x {args.turns_pass1} turns\n"
        f"[bold]Pass 2:[/bold] {len(args.seeds_pass2)} seeds x {args.turns_pass2} turns\n"
        f"[bold]Consensus:[/bold] {args.consensus}\n"
        f"[bold]Fallback:[/bold] {fallback_status}\n"
        f"[bold]Total runs:[/bold] {len(selected_cohorts) * (len(args.seeds_pass1) + len(args.seeds_pass2))}"
    )

    # Show full per-cohort eval config table up front
    def _fmt_fallback(model: str) -> str:
        chain = _resolve_fallback_chain(model)
        if not chain or args.no_fallback:
            return "[dim](none)[/dim]"
        return " → ".join(c.removeprefix("anthropic/").removeprefix("gemini/") for c in chain)

    runs_per_cohort = len(args.seeds_pass1) + len(args.seeds_pass2)
    p1_seeds_str = " ".join(str(s) for s in args.seeds_pass1)
    p2_seeds_str = " ".join(str(s) for s in args.seeds_pass2)

    design = Table(
        title="Cohort x Eval Configuration",
        title_style="bold cyan",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    design.add_column("#", justify="right", style="dim")
    design.add_column("Cohort", style="bold")
    design.add_column("Agent")
    design.add_column(f"Pass 1 harness LLM\n[dim]{len(args.seeds_pass1)} seed(s) x {args.turns_pass1} turns[/dim]")
    design.add_column("P1 fallback")
    design.add_column(f"Pass 2 harness LLM\n[dim]{len(args.seeds_pass2)} seed(s) x {args.turns_pass2} turns[/dim]")
    design.add_column("P2 fallback")
    design.add_column("Runs", justify="right")
    design.add_column("Predicted")
    for i, c in enumerate(selected_cohorts, 1):
        proxy_marker = " [yellow](proxy)[/yellow]" if c["uses_proxy_pass1"] else ""
        design.add_row(
            str(args.cohorts[i - 1]),
            f"{c['name']}\n[dim]{c['id']}[/dim]",
            c["agent_model"],
            f"{c['harness_llm_pass1']}{proxy_marker}\n[dim]seeds: {p1_seeds_str}[/dim]",
            _fmt_fallback(c["harness_llm_pass1"]),
            f"{c['harness_llm_pass2']}\n[dim]seeds: {p2_seeds_str}[/dim]",
            _fmt_fallback(c["harness_llm_pass2"]),
            str(runs_per_cohort),
            c["predicted_verdict"],
        )
    console.print()
    console.print(design)
    console.print(
        f"[dim]  Consensus: {args.consensus}  |  "
        f"Fallback: {'enabled' if not args.no_fallback else 'DISABLED'}  |  "
        f"Total runs across all cohorts: {len(selected_cohorts) * runs_per_cohort}[/dim]"
    )

    # ── Pre-flight: validate every LLM before burning eval budget ──────
    if not args.skip_preflight:
        ok, _errs = preflight_check_models(
            selected_cohorts,
            args.proxy_url_pass1,
            console,
        )
        if not ok:
            return 1

    benchmark_t0 = time.time()
    cohort_aggs: list[dict[str, Any]] = []

    for c in selected_cohorts:
        cohort_dir = benchmark_dir / c["id"]
        console.print()
        console.print(Rule(f"  ▶ COHORT — {c['name']}  ", style="bold green"))
        console.print(f"[dim]  {c['description']}[/dim]")
        console.print(f"[dim]  output: {cohort_dir.relative_to(Path.cwd())}[/dim]")

        cohort_t0 = time.time()
        agg = run_cohort(
            cohort=c,
            cohort_dir=cohort_dir,
            seeds_pass1=args.seeds_pass1,
            seeds_pass2=args.seeds_pass2,
            turns_pass1=args.turns_pass1,
            turns_pass2=args.turns_pass2,
            consensus=args.consensus,
            proxy_url=args.proxy_url_pass1,
            context_budget_pass1=args.context_budget_pass1,
            verbose=not args.quiet,
            console=console,
            enable_fallback=not args.no_fallback,
        )
        cohort_elapsed = time.time() - cohort_t0
        cohort_aggs.append(agg)
        console.print(
            f"\n[dim]  Cohort '{c['name']}' done in {cohort_elapsed/60:.1f} min "
            f"({agg['n_ok']}/{agg['n_runs']} OK)[/dim]"
        )

    benchmark_elapsed = time.time() - benchmark_t0

    console.print(Rule("Benchmark report", style="bold magenta"))
    render_per_cohort_table(cohort_aggs, console)
    comparisons = render_cross_cohort_comparison(cohort_aggs, console)

    write_benchmark_summary(
        benchmark_dir,
        cohort_aggs,
        comparisons,
        selected_cohorts,
        seeds_pass1=args.seeds_pass1,
        seeds_pass2=args.seeds_pass2,
        turns_pass1=args.turns_pass1,
        turns_pass2=args.turns_pass2,
        consensus=args.consensus,
        cohort_runtime_s=benchmark_elapsed,
    )

    console.print()
    console.print(Panel(
        f"[bold green]Benchmark complete[/bold green] in {benchmark_elapsed/60:.1f} min\n\n"
        f"Per-cohort folders + summaries: [bold]{benchmark_dir.relative_to(Path.cwd())}/[/bold]\n"
        f"Cross-cohort summary:           [bold]{benchmark_dir.relative_to(Path.cwd())}/benchmark_summary.md[/bold]\n",
        title="Output locations",
        border_style="green",
    ))

    return 0


if __name__ == "__main__":
    sys.exit(main())
