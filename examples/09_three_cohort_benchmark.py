"""Three-Cohort Benchmark — symmetric vs asymmetric harness/agent capability.

Runs **3 distinct Golden cohorts** back-to-back to characterize the harness's
behavior across the agent x harness-LLM capability matrix:

  Cohort 1 — Symmetric Frontier
    Pass 1 harness: gpt-4.1
    Pass 2 harness: anthropic/claude-sonnet-4-6
    Agent:          claude-opus-4-7         (frontier)
    Question:       What's the score ceiling? (cross-family judges, no
                    same-model self-eval bias)

  Cohort 2 — Asymmetric Cost-Floor
    Pass 1 harness: gemma-4-E4B-it-MLX-8bit (local proxy)
    Pass 2 harness: gpt-4o-mini             (cheap API)
    Agent:          claude-opus-4-7         (same frontier agent as Cohort 1)
    Question:       Can a cheap/local harness LLM cohort preserve the same
                    verdict as the frontier cohort on a strong agent?

  Cohort 3 — Asymmetric Sensitivity-Floor
    Pass 1 harness: gpt-4.1                 (same as Cohort 1)
    Pass 2 harness: anthropic/claude-sonnet-4-6 (same as Cohort 1)
    Agent:          gpt-4o-mini             (weak)
    Question:       Can the frontier-harness pair correctly identify a weak
                    agent? (lower bound on the harness's discrimination)

Each cohort runs **4 evals** (1 seed x 20 turns on pass 1, 3 seeds x 20 turns
on pass 2). 3 cohorts x 4 runs = **12 total runs**. Per-cohort outputs are
stored in their own subfolder for traceability:

    results/three_cohort_<timestamp>/
      01_symmetric_frontier/
        run_p1_seed42_turn20.json
        run_p2_seed42_turn20.json
        run_p2_seed7_turn20.json
        run_p2_seed100_turn20.json
        cohort_summary.json
        cohort_summary.md
      02_asymmetric_cost_floor/
        ... (4 runs + summary)
      03_asymmetric_sensitivity_floor/
        ... (4 runs + summary)
      benchmark_summary.json     (cross-cohort comparison)
      benchmark_summary.md       (Rich-rendered cross-cohort report)

Quickstart:
    # Default (you only need to point at your local proxy for Cohort 2)
    python examples/09_three_cohort_benchmark.py \\
        --proxy-url-pass1 http://localhost:1234/v1

    # Run only specific cohorts (e.g., skip Cohort 2 if no proxy available)
    python examples/09_three_cohort_benchmark.py --cohorts 1 3

    # Override seeds / turns
    python examples/09_three_cohort_benchmark.py \\
        --proxy-url-pass1 http://localhost:1234/v1 \\
        --seeds-pass1 42 --seeds-pass2 42 7 100 --turns 20

Backup / fallback strategy (default ON):
    This is an expensive pass — losing hours to one provider going down would
    be brutal. Each `run_one()` is wrapped in a fallback retry: if the primary
    harness LLM fails with a transient unavailability error (connection / 5xx /
    overloaded), the runner re-runs the WHOLE eval with a same-tier fallback
    model. Tiering is preserved (cheap-floor stays cheap, frontier stays
    frontier) so the cohort's intent isn't silently changed.

    Defined in MODEL_FALLBACK_CHAINS — examples:
      gemma-4-E4B-it-MLX-8bit (local) → gpt-4.1-mini → gpt-4o-mini
      gpt-4o-mini                     → gpt-4.1-mini → claude-haiku-4-5
      gpt-4.1                         → anthropic/claude-sonnet-4-6
      anthropic/claude-sonnet-4-6     → gpt-4.1
      claude-opus-4-7                 → anthropic/claude-sonnet-4-6 → gpt-4.1

    Rate-limit / quota errors are NOT swapped — those are recoverable on the
    same model with backoff (LiteLLM's built-in retry handles those). Fallback
    events are surfaced in the cohort summary + benchmark summary so you know
    when a "Cohort 2" result was actually scored by the fallback model.

    Disable with --no-fallback if you want strict primary-only behavior.

Required env:
    ANTHROPIC_API_KEY  (Sonnet harness LLM + Opus agent)
    OPENAI_API_KEY     (GPT-4.1 harness LLM + GPT-4o-mini agent)
    LM Studio / proxy  (Cohort 2 pass-1; only required when Cohort 2 selected)
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

# ── reuse helpers from 08_golden_adversarial.py (no duplication) ─────────────
_GOLDEN_PATH = Path(__file__).resolve().parent / "08_golden_adversarial.py"
_spec = importlib.util.spec_from_file_location("golden", _GOLDEN_PATH)
assert _spec and _spec.loader
_golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_golden)

run_one = _golden.run_one
aggregate = _golden.aggregate
ship_decision = _golden.ship_decision
_wire_proxy_for_harness_llm = _golden._wire_proxy_for_harness_llm
CANONICAL_METRICS = _golden.CANONICAL_METRICS
SHIP_GATE = _golden.SHIP_GATE

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


# ─────────────────────────────────────────────────────────────────────────────
# Backup / fallback strategy (this is an expensive pass — don't lose hours)
# ─────────────────────────────────────────────────────────────────────────────

# Fallback chains by capability tier. When a primary harness/agent LLM is
# unresponsive (transient connection / 5xx / overload errors), the runner
# retries the WHOLE evaluation with the next model in the chain. Cost may
# diverge from the primary if fallback is hit — surfaced in the cohort summary.
#
# Tiering preserves the cohort's *intent* — cheap-floor stays cheap, frontier
# stays frontier — so the experimental finding doesn't silently swap a cohort's
# capability tier.
MODEL_FALLBACK_CHAINS: dict[str, list[str]] = {
    # Local proxy (cheap-floor tier) -> cheap API fallback (un-proxies)
    "gemma-4-E4B-it-MLX-8bit": ["gpt-4.1-mini", "gpt-4o-mini"],

    # Cheap API tier
    "gpt-4o-mini": ["gpt-4.1-mini", "claude-haiku-4-5"],
    "gpt-4.1-mini": ["gpt-4o-mini", "claude-haiku-4-5"],
    "claude-haiku-4-5": ["gpt-4o-mini", "gpt-4.1-mini"],

    # Frontier harness tier (cross-family preserved)
    "gpt-4.1": ["anthropic/claude-sonnet-4-6"],
    "anthropic/claude-sonnet-4-6": ["gpt-4.1"],

    # Top-tier agent (rarely needs fallback but provided for completeness)
    "claude-opus-4-7": ["anthropic/claude-sonnet-4-6", "gpt-4.1"],
}

# Substrings (lowercased) in the error message that mark transient unavailability
# worth swapping models for. Quota / rate-limit errors are deliberately NOT here:
# those are recoverable on the same model with backoff. LiteLLM's built-in retry
# already handles short blips — fallback fires only when retries exhaust.
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


def _is_transient(error_str: str) -> bool:
    """Heuristic: does the error message look like transient unavailability?"""
    msg = error_str.lower()
    return any(s in msg for s in _TRANSIENT_ERROR_SUBSTRINGS)


def _resolve_fallback_chain(primary: str) -> list[str]:
    """Look up a fallback chain by primary model name.

    Strips the `openai/` wrapper that the proxy wiring may have added so a
    proxied Gemma resolves to the same chain as bare Gemma.
    """
    base = primary.removeprefix("openai/")
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
    """Run one eval; on transient harness LLM failure, swap to fallback chain.

    Pass-through `**run_kwargs` go straight to `run_one()`. Returns the same
    shape `run_one()` returns, with three extra keys when fallback fired:
    `fallback_used` (bool), `original_harness_llm`, `actual_harness_llm`.
    """
    chain = _resolve_fallback_chain(primary_harness_llm) if enable_fallback else []
    candidates = [primary_harness_llm, *chain]
    last_result: dict[str, Any] = {}

    for i, candidate in enumerate(candidates):
        is_fallback = i > 0
        # When falling back from a proxied primary to an API model, unwire the
        # proxy env so the API call routes to the real provider, not the proxy.
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
# Cohort definitions
# ─────────────────────────────────────────────────────────────────────────────

COHORTS: list[dict[str, Any]] = [
    {
        "id": "01_symmetric_frontier",
        "name": "Symmetric Frontier",
        "harness_llm_pass1": "gpt-4.1",
        "harness_llm_pass2": "anthropic/claude-sonnet-4-6",
        "agent_model": "claude-opus-4-7",
        "uses_proxy_pass1": False,
        "description": (
            "Frontier agent + cross-family frontier harness LLMs. Establishes "
            "the score ceiling on this benchmark."
        ),
        "predicted_verdict": "SILVER (mean ~8.0-8.7)",
    },
    {
        "id": "02_asymmetric_cost_floor",
        "name": "Asymmetric — Cost-Floor",
        "harness_llm_pass1": "gemma-4-E4B-it-MLX-8bit",
        "harness_llm_pass2": "gpt-4o-mini",
        "agent_model": "claude-opus-4-7",
        "uses_proxy_pass1": True,
        "description": (
            "Same frontier agent as Cohort 1, but cheap/local harness LLMs. "
            "Tests whether cost reduction preserves cert verdict."
        ),
        "predicted_verdict": "NEEDS_ENHANCEMENT or SILVER (mean ~7.5-9.0; over-rate +0.5-1.5 vs Cohort 1)",
    },
    {
        "id": "03_asymmetric_sensitivity_floor",
        "name": "Asymmetric — Sensitivity-Floor",
        "harness_llm_pass1": "gpt-4.1",
        "harness_llm_pass2": "anthropic/claude-sonnet-4-6",
        "agent_model": "gpt-4o-mini",
        "uses_proxy_pass1": False,
        "description": (
            "Weak agent + same frontier harness LLMs as Cohort 1. Tests the "
            "harness's lower bound — can strong judges correctly identify a "
            "weak agent? (Cohort-1-vs-3 gap = discrimination delta.)"
        ),
        "predicted_verdict": "NOT_READY (mean ~3-4)",
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

    # Snapshot OpenAI env vars so per-pass proxy wiring doesn't leak
    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")

    def _restore_env() -> None:
        _restore_proxy_env(saved_base, saved_key)

    total_evals = len(seeds_pass1) + len(seeds_pass2)

    def _eval_separator(eval_idx: int, label: str, seed: int, turns: int, model: str) -> None:
        """Heavy horizontal divider before each eval so it's easy to scroll/track."""
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

    # Collect fallback events (runs where the primary harness LLM was swapped)
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

    # Save cohort summary
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

    # Per-cohort markdown
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
                agg["agent_model"][:18],
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
            agg["agent_model"][:18],
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

    # Compact fallback callout under the cohort table
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
    """Render the headline cross-cohort comparisons."""
    by_id = {a["cohort_id"]: a for a in cohort_aggs}
    c1 = by_id.get("01_symmetric_frontier")
    c2 = by_id.get("02_asymmetric_cost_floor")
    c3 = by_id.get("03_asymmetric_sensitivity_floor")

    comparisons: dict[str, Any] = {}
    table = Table(
        title="Cross-cohort comparisons (the headline empirical findings)",
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

    if c1 and c2 and _mean(c1) is not None and _mean(c2) is not None:
        delta = _mean(c2) - _mean(c1)
        same_cert = _cert(c1) == _cert(c2)
        interp = (
            "✓ Cheap harness preserves cert tier" if same_cert
            else f"⚠ Cheap harness over-rates by ~{delta:+.2f} pts → cert tier diverged"
        )
        table.add_row(
            "Cohort 1 vs 2 — same agent, weaker harness LLMs",
            f"{delta:+.2f}",
            "same" if same_cert else "differ",
            interp,
        )
        comparisons["c1_vs_c2"] = {
            "delta_mean": delta, "same_cert": same_cert,
            "c1_cert": _cert(c1), "c2_cert": _cert(c2),
        }

    if c1 and c3 and _mean(c1) is not None and _mean(c3) is not None:
        delta = _mean(c3) - _mean(c1)
        diff_cert = _cert(c1) != _cert(c3)
        interp = (
            f"✓ Harness discriminates by {abs(delta):.2f} pts (different cert)" if diff_cert
            else "⚠ Harness collapsed weak vs strong agent into same cert tier — discrimination failure"
        )
        table.add_row(
            "Cohort 1 vs 3 — same harness LLMs, weaker agent",
            f"{delta:+.2f}",
            "differ ✓" if diff_cert else "same ⚠",
            interp,
        )
        comparisons["c1_vs_c3"] = {
            "delta_mean": delta, "diff_cert": diff_cert,
            "c1_cert": _cert(c1), "c3_cert": _cert(c3),
        }

    if c2 and c3 and _mean(c2) is not None and _mean(c3) is not None:
        delta = _mean(c3) - _mean(c2)
        diff_cert = _cert(c2) != _cert(c3)
        table.add_row(
            "Cohort 2 vs 3 — opposite-end ablations",
            f"{delta:+.2f}",
            "differ" if diff_cert else "same",
            "Cross-check — verifies the harness reads weak agent as weak even via cheap judges",
        )
        comparisons["c2_vs_c3"] = {
            "delta_mean": delta, "diff_cert": diff_cert,
            "c2_cert": _cert(c2), "c3_cert": _cert(c3),
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

    # Markdown report
    md = ["# Three-Cohort Benchmark — Summary", "", f"Generated: {summary['generated_at']}"]
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
        if key == "c1_vs_c2":
            md.append(
                f"- **Cohort 1 → 2 (cost-floor):** Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['c1_cert']} → {c['c2_cert']} "
                f"({'preserved ✓' if c['same_cert'] else 'diverged ⚠'})"
            )
        elif key == "c1_vs_c3":
            md.append(
                f"- **Cohort 1 → 3 (sensitivity-floor):** Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['c1_cert']} → {c['c3_cert']} "
                f"({'differs ✓' if c['diff_cert'] else 'same ⚠'})"
            )
        elif key == "c2_vs_c3":
            md.append(
                f"- **Cohort 2 vs 3 (cross-check):** Δ mean = {c['delta_mean']:+.2f} pts; "
                f"cert {c['c2_cert']} vs {c['c3_cert']}"
            )
    md.append("")

    # Fallback events (across all cohorts)
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
            "Three-Cohort Benchmark — symmetric vs asymmetric harness/agent "
            "capability characterization. 3 cohorts x 4 runs = 12 runs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--cohorts",
        type=int,
        nargs="+",
        choices=[1, 2, 3],
        default=[1, 2, 3],
        help="Which cohorts to run (1-indexed). Default: all 3.",
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
        help="OpenAI-compatible proxy URL for Cohort 2's pass-1 harness LLM "
             "(local Gemma). Required when running Cohort 2.",
    )
    p.add_argument(
        "--context-budget-pass1", type=int, default=None,
        help="Max prompt tokens for pass-1 harness LLM calls. Required when "
             "Cohort 2's local proxy has small loaded context (e.g., 6000 for "
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
        help="Disable model-fallback retry on transient unavailability "
             "(connection / 5xx / overloaded). Default: fallback enabled — "
             "if a model goes down mid-pass the runner swaps to a same-tier "
             "model (e.g. local Gemma → gpt-4.1-mini) so the expensive pass "
             "completes. Fallback events are surfaced in the cohort summary.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    console = Console()

    # Build the output benchmark folder
    tag = args.output_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    benchmark_dir = RESULTS_DIR / f"three_cohort_{tag}"
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    selected_cohorts = [COHORTS[i - 1] for i in args.cohorts]

    console.print(Rule("Three-Cohort Benchmark", style="bold magenta"))
    fallback_status = (
        "[red]disabled[/red]" if args.no_fallback
        else "[green]enabled[/green] (transient failures swap to same-tier model)"
    )
    console.print(
        f"\n[bold]Output folder:[/bold] {benchmark_dir.relative_to(Path.cwd())}\n"
        f"[bold]Cohorts to run:[/bold] {len(selected_cohorts)} of 3\n"
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
        return " → ".join(c.removeprefix("anthropic/") for c in chain)

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

    # ── Cross-cohort report ─────────────────────────────────────────────
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
