"""Multi-Agent Benchmark — 4 cohorts x 4 application agents.

Tests **4 production-style application agents** (medical triage, privacy/
security/GDPR-CCPA, code generation, customer support) across **4 cohort
configurations** (2 symmetric strong-juror + 2 asymmetric cheap-juror) using
**2 strong frontier agent LLMs** (GPT-5.5, Claude Opus 4.7).

Cohort matrix (each cell runs P1 + P2 cross-juror passes):

  +------------+--------------------------------+--------------------------------+
  |            | Strong jurors                  | Cheap jurors                   |
  +------------+--------------------------------+--------------------------------+
  | gpt-5.5    | Sym-GPT5                       | Asym-GPT5                      |
  |   agent    | P1=opus-4-7  P2=grok-4.3       | P1=haiku-4-5  P2=grok-4.1-fast |
  +------------+--------------------------------+--------------------------------+
  | opus-4-7   | Sym-Opus                       | Asym-Opus                      |
  |   agent    | P1=gpt-5.5  P2=grok-4.3        | P1=4o-mini  P2=grok-4.1-fast   |
  +------------+--------------------------------+--------------------------------+

Per-cohort: each agent runs TWO 20-turn evaluations (P1 + P2 cross-juror
triangulation) with the SAME seed so the agent's transcript is comparable
between jurors. Per-cell seed is unique across the matrix:
   seed = base_seed * 1000 + cohort_index * 100 + agent_index
Total: 4 cohorts x 4 agents x 2 passes x 20 turns = **32 evaluations**.

Cross-family rule preserved per cohort: the agent's own family is excluded
from the juror role.

Output structure (each cell stored separately for analysis):

    results/multi_agent_benchmark_<timestamp>/
      Sym_GPT5/
        medical_triage_assistant.json    (full transcript + scores)
        privacy_security_agent.json
        code_generation_agent.json
        customer_support_agent.json
        cohort_summary.{json,md}
      Sym_Opus/    (same shape)
      Asym_GPT5/   (same shape)
      Asym_Opus/   (same shape)
      benchmark_summary.json    (4-cohort x 4-agent matrix)
      benchmark_summary.md      (Rich-rendered cross-cohort report)

Quickstart:
    python examples/11_multi_agent_benchmark.py

Required env:
    OPENAI_API_KEY       (gpt-5.5, gpt-4o-mini)
    ANTHROPIC_API_KEY    (claude-opus-4-7, claude-haiku-4-5)
    XAI_API_KEY          (gemini/gemini-2.5-pro in symmetric, gemma-4-E4B-it-MLX-8bit in asymmetric)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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

from proofagent_harness import Harness

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from examples.agents import (
    AgentSpec,
    load_agent_spec,
    make_agent_from_spec,
    make_context_from_spec,
)

_GOLDEN_PATH = Path(__file__).resolve().parent / "08_golden_adversarial.py"
_spec = importlib.util.spec_from_file_location("golden", _GOLDEN_PATH)
assert _spec and _spec.loader
_golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_golden)

ship_decision = _golden.ship_decision
CANONICAL_METRICS = _golden.CANONICAL_METRICS
SHIP_GATE = _golden.SHIP_GATE
_wire_proxy_for_harness_llm = _golden._wire_proxy_for_harness_llm

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
AGENTS_DIR = Path(__file__).resolve().parent / "agents"


def _restore_proxy_env(saved_base: str | None, saved_key: str | None) -> None:
    """Restore the OpenAI env vars that were live before proxy wiring."""
    if saved_base is None:
        os.environ.pop("OPENAI_BASE_URL", None)
    else:
        os.environ["OPENAI_BASE_URL"] = saved_base
    if saved_key is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = saved_key


# ─────────────────────────────────────────────────────────────────────────────
# Cohort + agent spec definitions
# ─────────────────────────────────────────────────────────────────────────────

COHORTS: list[dict[str, Any]] = [
    {
        "id": "Sym_GPT5",
        "name": "Sym-GPT5 (strong jurors, GPT-5.5 agent)",
        "agent_model": "gpt-5.5",
        "harness_llm_pass1": "claude-opus-4-7",
        "harness_llm_pass2": "gemini/gemini-2.5-pro",
        "uses_proxy_pass2": False,
        "type": "symmetric",
        "out_of_pool_family": "OpenAI",
        "description": "Symmetric: strong cross-family jurors (Opus 4.7 + Gemini 2.5 Pro) judging strong GPT-5.5 agent. No OpenAI juror.",
    },
    {
        "id": "Sym_Opus",
        "name": "Sym-Opus (strong jurors, Opus 4.7 agent)",
        "agent_model": "claude-opus-4-7",
        "harness_llm_pass1": "gpt-5.5",
        "harness_llm_pass2": "gemini/gemini-2.5-pro",
        "uses_proxy_pass2": False,
        "type": "symmetric",
        "out_of_pool_family": "Anthropic",
        "description": "Symmetric: strong cross-family jurors (GPT-5.5 + Gemini 2.5 Pro) judging strong Opus 4.7 agent. No Anthropic juror.",
    },
    {
        "id": "Asym_GPT5",
        "name": "Asym-GPT5 (cheap jurors, GPT-5.5 agent)",
        "agent_model": "gpt-5.5",
        "harness_llm_pass1": "anthropic/claude-haiku-4-5",
        "harness_llm_pass2": "gemma-4-E4B-it-MLX-8bit",
        "uses_proxy_pass2": True,
        "type": "asymmetric",
        "out_of_pool_family": "OpenAI",
        "description": "Asymmetric: cheap cross-family jurors (Haiku 4.5 + local Gemma 4B via proxy) judging strong GPT-5.5 agent. Tests cost-floor calibration drift.",
    },
    {
        "id": "Asym_Opus",
        "name": "Asym-Opus (cheap jurors, Opus 4.7 agent)",
        "agent_model": "claude-opus-4-7",
        "harness_llm_pass1": "gpt-4o-mini",
        "harness_llm_pass2": "gemma-4-E4B-it-MLX-8bit",
        "uses_proxy_pass2": True,
        "type": "asymmetric",
        "out_of_pool_family": "Anthropic",
        "description": "Asymmetric: cheap cross-family jurors (gpt-4o-mini + local Gemma 4B via proxy) judging strong Opus 4.7 agent. Tests cost-floor calibration drift.",
    },
]


# Fallback chains for transient unavailability (connection / 5xx / overloaded).
# Rate-limit errors are NOT swapped (those should retry on same model).
MODEL_FALLBACK_CHAINS: dict[str, list[str]] = {
    # Frontier juror tier
    "gpt-5.5": ["gpt-5.4", "gpt-4.1"],
    "claude-opus-4-7": ["anthropic/claude-sonnet-4-6", "gpt-4.1"],
    "grok-4.3": ["xai/grok-4.20-0309-non-reasoning", "anthropic/claude-sonnet-4-6"],
    "gemini-2.5-pro": ["gemini/gemini-2.0-flash", "anthropic/claude-sonnet-4-6"],
    # Cheap juror tier
    "claude-haiku-4-5": ["gpt-4o-mini", "gpt-4.1-mini"],
    "gpt-4o-mini": ["gpt-4.1-mini", "anthropic/claude-haiku-4-5"],
    "grok-4-1-fast-non-reasoning": ["xai/grok-4-1-fast-reasoning", "anthropic/claude-haiku-4-5"],
    # Local proxied (cheap-floor)
    "gemma-4-E4B-it-MLX-8bit": ["anthropic/claude-haiku-4-5", "gpt-4o-mini"],
}

AGENT_SPEC_PATHS: list[Path] = [
    AGENTS_DIR / "medical_triage_assistant.json",
    AGENTS_DIR / "privacy_security_agent.json",
    AGENTS_DIR / "code_generation_agent.json",
    AGENTS_DIR / "customer_support_agent.json",
]


def _derive_seed(base_seed: int, cohort_idx: int, agent_idx: int) -> int:
    """Each (cohort, agent) cell gets a unique reproducible seed so all 16
    cells see different trap rotations; whole benchmark replayable from
    --base-seed."""
    return base_seed * 1000 + cohort_idx * 100 + agent_idx


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight LLM check
# ─────────────────────────────────────────────────────────────────────────────

def _required_envs(models: list[str]) -> set[str]:
    needed: set[str] = set()
    for m in models:
        ml = m.lower()
        if "claude" in ml or "anthropic" in ml:
            needed.add("ANTHROPIC_API_KEY")
        if "gemini" in ml:
            needed.add("GEMINI_API_KEY")
        if "xai/" in ml or ml.startswith("grok"):
            needed.add("XAI_API_KEY")
        if "gpt" in ml or ml.startswith("openai/") or "gpt-" in ml:
            needed.add("OPENAI_API_KEY")
    return needed


def _ping_litellm(model: str) -> str:
    """Tiny LLM ping for pre-flight reachability. Handles GPT-5.x/o-series
    reasoning-model param differences and Gemini/Anthropic thinking-model
    output budgets."""
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
        base_kwargs["max_tokens"] = 512 if is_thinking else 64
        if not is_thinking:
            base_kwargs["temperature"] = 0

    try:
        r = litellm.completion(**base_kwargs)
        return (r.choices[0].message.content or "").strip()
    except Exception as exc:
        msg = str(exc).lower()
        mutated = False
        if (
            "max_tokens" in msg
            and "max_completion_tokens" in msg
            and "max_tokens" in base_kwargs
        ):
            base_kwargs["max_completion_tokens"] = max(256, base_kwargs.pop("max_tokens", 64))
            mutated = True
        if (
            "temperature" in msg
            and any(s in msg for s in ("does not support", "unsupported", "deprecated"))
            and "temperature" in base_kwargs
        ):
            base_kwargs.pop("temperature", None)
            mutated = True
        if not mutated:
            raise
        r = litellm.completion(**base_kwargs)
        return (r.choices[0].message.content or "").strip()


def preflight_check(
    cohorts: list[dict[str, Any]],
    specs: list[AgentSpec],
    console: Console,
    proxy_url_pass2: str | None = None,
) -> tuple[bool, list[str]]:
    """Ping every unique LLM and verify every agent constructor BEFORE running.

    Catches: missing env vars, bad model names, unreachable providers,
    auth failures, agent-routing bugs. Saves hours of wasted compute.
    """
    console.print()
    console.print(Rule("Pre-flight LLM check (all models)", style="bold yellow"))

    all_models = []
    for c in cohorts:
        all_models.extend([c["agent_model"], c["harness_llm_pass1"], c["harness_llm_pass2"]])
    needed = _required_envs(all_models)
    env_errors = [f"  Missing env var: {v}" for v in sorted(needed) if not os.environ.get(v)]
    if env_errors:
        console.print()
        console.print("[bold red]Environment variable errors:[/bold red]")
        for e in env_errors:
            console.print(f"[red]{e}[/red]")
        console.print()
        console.print(Panel(
            "\n".join(env_errors) + "\n\nSet them in your shell, then re-run.",
            title="[red]Pre-flight FAILED — aborting before any eval runs[/red]",
            border_style="red",
        ))
        return False, env_errors

    # Each entry: (model_name, role_label, is_proxied)
    unique_pings: set[tuple[str, str, bool]] = set()
    for c in cohorts:
        unique_pings.add((c["agent_model"], "agent", False))
        unique_pings.add((c["harness_llm_pass1"], "harness P1", False))
        unique_pings.add(
            (c["harness_llm_pass2"], "harness P2", c.get("uses_proxy_pass2", False))
        )

    console.print(
        f"\n[dim]Pinging {len(unique_pings)} unique model/role combinations "
        f"(short prompt, ~30s timeout each)[/dim]\n"
    )

    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")

    errors: list[str] = []
    for model, role, is_proxied in sorted(unique_pings):
        target_model = model
        display = model
        if is_proxied:
            if not proxy_url_pass2:
                errors.append(
                    f"  {model} ({role}): proxy URL required but --proxy-url-pass2 not provided"
                )
                console.print(
                    f"[red]  ✗ {model} ({role}): --proxy-url-pass2 missing[/red]"
                )
                continue
            target_model = _wire_proxy_for_harness_llm(proxy_url_pass2, model)
            display = f"{model} (via proxy)"
        try:
            content = _ping_litellm(target_model)
            if not content:
                err = f"  {target_model} ({role}): empty response"
                errors.append(err)
                console.print(f"[red]  ✗ {display} ({role}): empty response[/red]")
            else:
                console.print(f"[green]  ✓ {display} ({role}): {content[:30]!r}[/green]")
        except Exception as exc:
            short = str(exc)[:200].replace("\n", " ")
            errors.append(f"  {target_model} ({role}): {type(exc).__name__}: {short}")
            console.print(f"[red]  ✗ {display} ({role}): {type(exc).__name__}: {short[:120]}[/red]")
        finally:
            if is_proxied:
                _restore_proxy_env(saved_base, saved_key)

    console.print()
    console.print("[dim]Verifying agent constructor for each unique (agent_model x agent_spec) combination...[/dim]")
    unique_agents: set[tuple[str, str]] = set()
    for c in cohorts:
        for spec in specs:
            unique_agents.add((c["agent_model"], spec.name))
    for agent_model, spec_name in sorted(unique_agents):
        spec = next(s for s in specs if s.name == spec_name)
        try:
            agent = make_agent_from_spec(spec, agent_model)
            r = agent("Reply with just: OK")
            text = (r.text or "").strip()
            if not text:
                errors.append(f"  agent({agent_model}, {spec_name}): empty response")
                console.print(f"[red]  ✗ agent({agent_model}, {spec_name}): empty response[/red]")
            else:
                console.print(f"[green]  ✓ agent({agent_model}, {spec_name}): {text[:30]!r}[/green]")
        except Exception as exc:
            short = str(exc)[:200].replace("\n", " ")
            errors.append(f"  agent({agent_model}, {spec_name}): {type(exc).__name__}: {short}")
            console.print(f"[red]  ✗ agent({agent_model}, {spec_name}): {type(exc).__name__}: {short[:120]}[/red]")

    if errors:
        console.print()
        console.print(Panel(
            "\n".join(errors[:20])
            + (f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""),
            title="[red]Pre-flight FAILED — aborting before any eval runs[/red]",
            border_style="red",
        ))
        return False, errors

    console.print()
    console.print("[bold green]✓ All pre-flight checks passed — proceeding with benchmark[/bold green]")
    return True, []


# ─────────────────────────────────────────────────────────────────────────────
# Per-cell evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _run_one_pass(
    *,
    cohort: dict[str, Any],
    spec: AgentSpec,
    pass_label: str,
    harness_llm: str,
    seed: int,
    turns: int,
    consensus: str,
    output_path: Path,
    console: Console,
    verbose: bool,
) -> dict[str, Any]:
    """Run one pass (P1 or P2) for one cell. Saves the harness report JSON to
    output_path and returns a normalized result dict."""
    console.print(
        f"  [cyan]▶[/cyan] [{pass_label}] juror={harness_llm}  seed={seed}  "
        f"turns={turns}  consensus={consensus}"
    )
    t0 = time.time()
    try:
        agent_callable = make_agent_from_spec(spec, cohort["agent_model"])
        agent_context = make_context_from_spec(spec)
        report = Harness(
            llm=harness_llm,
            turns=turns,
            consensus=consensus,
            seed=seed,
            verbose=verbose,
        ).evaluate(
            agent_callable,
            role=spec.role,
            business_case=spec.business_case,
            goal=spec.goal,
            context=agent_context,
        )
    except Exception as exc:
        elapsed = time.time() - t0
        console.print(
            f"    [red]✗ FAILED[/red] after {elapsed:.0f}s — "
            f"{type(exc).__name__}: {exc}"
        )
        return {
            "cohort_id": cohort["id"],
            "agent_name": spec.name,
            "agent_domain": spec.domain,
            "agent_model": cohort["agent_model"],
            "pass": pass_label,
            "harness_llm": harness_llm,
            "seed": seed,
            "turns": turns,
            "error": f"{type(exc).__name__}: {exc}",
            "duration": elapsed,
        }
    elapsed = time.time() - t0
    report.to_json(str(output_path))

    result = {
        "cohort_id": cohort["id"],
        "agent_name": spec.name,
        "agent_domain": spec.domain,
        "agent_model": cohort["agent_model"],
        "pass": pass_label,
        "harness_llm": harness_llm,
        "seed": seed,
        "turns": turns,
        "final_score": report.final_score,
        "certification": report.certification.value,
        "per_metric": dict(report.per_metric),
        "warnings": list(report.warnings),
        "n_findings": len(report.findings),
        "n_critical_findings": sum(1 for f in report.findings if f.severity.value == "critical"),
        "duration": elapsed,
        "tokens": report.tokens_used,
        "report_path": str(output_path),
    }
    cert_color = {
        "GOLD": "yellow",
        "SILVER": "bright_white",
        "NEEDS_ENHANCEMENT": "yellow3",
        "NOT_READY": "red",
    }.get(result["certification"], "white")
    console.print(
        f"    [green]✓[/green] {result['final_score']:.2f}  "
        f"[{cert_color}]{result['certification']}[/{cert_color}]  "
        f"({elapsed:.0f}s, {result['tokens']:,} tokens, {result['n_findings']} findings)"
    )
    return result


def run_one_cell(
    *,
    cohort: dict[str, Any],
    spec: AgentSpec,
    seed: int,
    turns: int,
    consensus: str,
    cell_dir: Path,
    console: Console,
    verbose: bool = True,
    proxy_url_pass2: str | None = None,
) -> list[dict[str, Any]]:
    """Run one (cohort, agent) cell with BOTH P1 and P2 cross-juror passes.
    Same seed for both so the agent transcript is comparable. Returns a list
    of two result dicts (P1 + P2). Wires the OpenAI-compatible proxy for P2
    when the cohort declares uses_proxy_pass2 (e.g., local Gemma juror)."""
    cell_dir.mkdir(parents=True, exist_ok=True)
    p1_path = cell_dir / f"{spec.name}_p1.json"
    p2_path = cell_dir / f"{spec.name}_p2.json"

    console.print()
    console.print(
        f"[bold cyan]▶[/bold cyan] {cohort['id']} x {spec.name}  "
        f"agent={cohort['agent_model']}  seed={seed}  turns={turns}  "
        f"consensus={consensus}"
    )

    p1 = _run_one_pass(
        cohort=cohort, spec=spec, pass_label="P1",
        harness_llm=cohort["harness_llm_pass1"],
        seed=seed, turns=turns, consensus=consensus,
        output_path=p1_path, console=console, verbose=verbose,
    )

    # P2 may need proxy wiring (local Gemma in asym cohorts)
    p2_harness = cohort["harness_llm_pass2"]
    is_p2_proxied = cohort.get("uses_proxy_pass2", False)
    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")
    if is_p2_proxied:
        if not proxy_url_pass2:
            raise SystemExit(
                f"Cohort '{cohort['name']}' P2 requires --proxy-url-pass2 "
                f"(harness LLM pass 2 = {p2_harness})."
            )
        p2_harness = _wire_proxy_for_harness_llm(proxy_url_pass2, cohort["harness_llm_pass2"])
        console.print(
            f"  [dim][config] P2 proxy: {proxy_url_pass2} → harness LLM={p2_harness}[/dim]"
        )
    try:
        p2 = _run_one_pass(
            cohort=cohort, spec=spec, pass_label="P2",
            harness_llm=p2_harness,
            seed=seed, turns=turns, consensus=consensus,
            output_path=p2_path, console=console, verbose=verbose,
        )
    finally:
        if is_p2_proxied:
            _restore_proxy_env(saved_base, saved_key)
    return [p1, p2]


# ─────────────────────────────────────────────────────────────────────────────
# Per-cohort runner (loops over agents)
# ─────────────────────────────────────────────────────────────────────────────

def run_cohort(
    *,
    cohort: dict[str, Any],
    cohort_idx: int,
    specs: list[AgentSpec],
    base_seed: int,
    turns: int,
    consensus: str,
    cohort_dir: Path,
    console: Console,
    verbose: bool = True,
    proxy_url_pass2: str | None = None,
) -> dict[str, Any]:
    """Run one cohort across all agent specs. Returns aggregated dict."""
    cohort_dir.mkdir(parents=True, exist_ok=True)
    cell_results: list[dict[str, Any]] = []

    console.print()
    console.print(Rule(f"  ▶ COHORT — {cohort['name']}  ", style="bold green"))
    console.print(f"[dim]  {cohort['description']}[/dim]")
    console.print(f"[dim]  output: {cohort_dir.relative_to(Path.cwd())}[/dim]")

    for agent_idx, spec in enumerate(specs):
        seed = _derive_seed(base_seed, cohort_idx, agent_idx)
        console.print()
        console.print(
            Rule(
                f"  CELL {agent_idx + 1}/{len(specs)} — {cohort['id']} x "
                f"{spec.name} (P1 + P2)  ",
                style="bold blue",
            )
        )
        # Each cell returns 2 results (P1 + P2); flatten into cell_results
        cell_results.extend(run_one_cell(
            cohort=cohort,
            spec=spec,
            seed=seed,
            turns=turns,
            consensus=consensus,
            cell_dir=cohort_dir,
            console=console,
            verbose=verbose,
            proxy_url_pass2=proxy_url_pass2,
        ))

    ok = [c for c in cell_results if "error" not in c]
    failed = [c for c in cell_results if "error" in c]

    scores = [c["final_score"] for c in ok]
    overall = {
        "n_cells": len(cell_results),
        "n_ok": len(ok),
        "n_failed": len(failed),
        "mean": sum(scores) / len(scores) if scores else 0.0,
        "min": min(scores) if scores else 0.0,
        "max": max(scores) if scores else 0.0,
        "spread": (max(scores) - min(scores)) if scores else 0.0,
    }

    cert_tally: dict[str, int] = {}
    for c in ok:
        cert_tally[c["certification"]] = cert_tally.get(c["certification"], 0) + 1

    per_metric: dict[str, dict[str, float]] = {}
    for m in CANONICAL_METRICS:
        vals = [c["per_metric"].get(m, 0.0) for c in ok if m in c["per_metric"]]
        if vals:
            per_metric[m] = {
                "mean": sum(vals) / len(vals),
                "min": min(vals),
                "max": max(vals),
            }

    summary = {
        "cohort_id": cohort["id"],
        "cohort_name": cohort["name"],
        "type": cohort["type"],
        "agent_model": cohort["agent_model"],
        "harness_llm_pass1": cohort["harness_llm_pass1"],
        "harness_llm_pass2": cohort["harness_llm_pass2"],
        "out_of_pool_family": cohort["out_of_pool_family"],
        "description": cohort["description"],
        "turns": turns,
        "consensus": consensus,
        "overall": overall,
        "cert_tally": cert_tally,
        "per_metric": per_metric,
        "cells": cell_results,
    }

    (cohort_dir / "cohort_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    md_lines = [
        f"# Cohort: {cohort['name']}",
        "",
        f"**ID:** `{cohort['id']}`  |  **Type:** `{cohort['type']}`  |  "
        f"**Out-of-pool family:** `{cohort['out_of_pool_family']}`",
        f"**Agent model:** `{cohort['agent_model']}`",
        f"**Pass 1 juror:** `{cohort['harness_llm_pass1']}`  |  "
        f"**Pass 2 juror:** `{cohort['harness_llm_pass2']}`",
        f"**Turns:** {turns}  |  **Consensus:** `{consensus}`",
        "",
        f"_{cohort['description']}_",
        "",
        "## Per-agent x per-pass results",
        "",
        "| Agent | Domain | Pass | Juror | Score | Cert | Findings | Tokens | Dur | Seed |",
        "|---|---|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for c in cell_results:
        if "error" in c:
            md_lines.append(
                f"| {c['agent_name']} | — | {c['pass']} | "
                f"`{c['harness_llm']}` | ERROR | — | — | — | "
                f"{c['duration']:.0f}s | {c['seed']} |"
            )
        else:
            md_lines.append(
                f"| {c['agent_name']} | {c['agent_domain']} | {c['pass']} | "
                f"`{c['harness_llm']}` | {c['final_score']:.2f} | "
                f"{c['certification']} | "
                f"{c['n_findings']} ({c['n_critical_findings']} crit) | "
                f"{c['tokens']:,} | {c['duration']:.0f}s | {c['seed']} |"
            )
    md_lines.extend([
        "",
        "## Cohort statistics",
        "",
        f"- **Mean:** {overall['mean']:.2f}",
        f"- **Min:** {overall['min']:.2f}",
        f"- **Max:** {overall['max']:.2f}",
        f"- **Spread:** {overall['spread']:.2f}",
        f"- **Cert tally:** {dict(cert_tally)}",
        "",
        "## Per-metric across all agents in this cohort",
        "",
        "| Metric | Mean | Min | Max |",
        "|---|---:|---:|---:|",
    ])
    for m, s in per_metric.items():
        md_lines.append(f"| {m} | {s['mean']:.2f} | {s['min']:.1f} | {s['max']:.1f} |")
    md_lines.append("")
    (cohort_dir / "cohort_summary.md").write_text("\n".join(md_lines))

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Cross-cohort summary (4-cohort x 4-agent matrix)
# ─────────────────────────────────────────────────────────────────────────────

def render_matrix_table(
    cohort_summaries: list[dict[str, Any]],
    spec_names: list[str],
    console: Console,
) -> None:
    """Render the 4-cohort x 4-agent score matrix as a Rich table."""
    table = Table(
        title="4-cohort x 4-agent score matrix",
        title_style="bold cyan",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("Agent", style="bold")
    for cs in cohort_summaries:
        label = (
            f"{cs['cohort_id']}\n[dim]{cs['agent_model'][:18]}[/dim]\n"
            f"[dim]juror={cs['harness_llm'][:18]}[/dim]"
        )
        table.add_column(label, justify="right")
    table.add_column("Agent mean\n(across cohorts)", justify="right", style="bold")

    # Group cells by (cohort, agent), aggregating across P1 + P2 passes
    by_cohort_agent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for cs in cohort_summaries:
        for cell in cs["cells"]:
            key = (cs["cohort_id"], cell["agent_name"])
            by_cohort_agent.setdefault(key, []).append(cell)

    for spec_name in spec_names:
        row = [spec_name]
        agent_means = []
        for cs in cohort_summaries:
            passes = by_cohort_agent.get((cs["cohort_id"], spec_name), [])
            ok_passes = [p for p in passes if "error" not in p]
            if not ok_passes:
                row.append(Text("ERR", style="red"))
                continue
            mean = sum(p["final_score"] for p in ok_passes) / len(ok_passes)
            agent_means.append(mean)
            # Choose cert from majority across passes
            cert = max(
                {p["certification"] for p in ok_passes},
                key=lambda c: sum(1 for p in ok_passes if p["certification"] == c),
            )
            color = {
                "GOLD": "yellow",
                "SILVER": "bright_white",
                "NEEDS_ENHANCEMENT": "yellow3",
                "NOT_READY": "red",
            }.get(cert, "white")
            row.append(Text(f"{mean:.2f} {cert[:4]}", style=color))
        if agent_means:
            row.append(f"{sum(agent_means) / len(agent_means):.2f}")
        else:
            row.append("—")
        table.add_row(*row)

    means_row = [Text("Cohort mean", style="bold dim")]
    for cs in cohort_summaries:
        means_row.append(Text(f"{cs['overall']['mean']:.2f}", style="bold"))
    means_row.append("")
    table.add_row(*means_row)

    console.print()
    console.print(table)


def write_benchmark_summary(
    benchmark_dir: Path,
    cohort_summaries: list[dict[str, Any]],
    spec_names: list[str],
    *,
    base_seed: int,
    turns: int,
    consensus: str,
    runtime_s: float,
) -> None:
    """Write benchmark_summary.{json,md} with the matrix view + cohort deltas."""
    # Group cells by (cohort, agent) — list of passes per cell
    by_cohort_agent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for cs in cohort_summaries:
        for cell in cs["cells"]:
            key = (cs["cohort_id"], cell["agent_name"])
            by_cohort_agent.setdefault(key, []).append(cell)

    def cell_mean(cohort_id: str, spec_name: str) -> float | None:
        passes = by_cohort_agent.get((cohort_id, spec_name), [])
        ok = [p for p in passes if "error" not in p]
        if not ok:
            return None
        return sum(p["final_score"] for p in ok) / len(ok)

    cohort_ids = [cs["cohort_id"] for cs in cohort_summaries]

    # Compute cost-floor deltas (Sym vs Asym for same agent model)
    deltas: dict[str, Any] = {}
    sym_gpt5 = next((c for c in cohort_summaries if c["cohort_id"] == "Sym_GPT5"), None)
    asym_gpt5 = next((c for c in cohort_summaries if c["cohort_id"] == "Asym_GPT5"), None)
    sym_opus = next((c for c in cohort_summaries if c["cohort_id"] == "Sym_Opus"), None)
    asym_opus = next((c for c in cohort_summaries if c["cohort_id"] == "Asym_Opus"), None)

    if sym_gpt5 and asym_gpt5 and sym_gpt5["overall"]["n_ok"] and asym_gpt5["overall"]["n_ok"]:
        deltas["gpt55_cost_floor"] = {
            "sym_mean": sym_gpt5["overall"]["mean"],
            "asym_mean": asym_gpt5["overall"]["mean"],
            "delta_mean": asym_gpt5["overall"]["mean"] - sym_gpt5["overall"]["mean"],
        }
    if sym_opus and asym_opus and sym_opus["overall"]["n_ok"] and asym_opus["overall"]["n_ok"]:
        deltas["opus47_cost_floor"] = {
            "sym_mean": sym_opus["overall"]["mean"],
            "asym_mean": asym_opus["overall"]["mean"],
            "delta_mean": asym_opus["overall"]["mean"] - sym_opus["overall"]["mean"],
        }

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runtime_seconds": runtime_s,
        "config": {
            "base_seed": base_seed,
            "turns": turns,
            "consensus": consensus,
            "n_cohorts": len(cohort_summaries),
            "n_agents": len(spec_names),
            "n_cells": len(cohort_summaries) * len(spec_names),
        },
        "cohorts": [
            {k: v for k, v in cs.items() if k != "cells"}
            for cs in cohort_summaries
        ],
        "matrix": {
            cohort_id: {
                spec_name: {
                    "passes": by_cohort_agent.get((cohort_id, spec_name), []),
                    "cell_mean": cell_mean(cohort_id, spec_name),
                }
                for spec_name in spec_names
            }
            for cohort_id in cohort_ids
        },
        "deltas": deltas,
    }
    (benchmark_dir / "benchmark_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    md = ["# Multi-Agent Benchmark — Summary", "", f"Generated: {summary['generated_at']}"]
    md.append("")
    md.append(
        f"**Config:** {len(cohort_summaries)} cohorts x {len(spec_names)} agents "
        f"= {len(cohort_summaries) * len(spec_names)} cells, "
        f"{turns} turns/cell, base_seed={base_seed}, consensus=`{consensus}`."
    )
    md.append(f"**Runtime:** {runtime_s/60:.1f} min")
    md.append("")
    md.append("## Cohort design (P1 + P2 cross-juror per cell)")
    md.append("")
    md.append("| Cohort | Type | Agent LLM | P1 juror | P2 juror | Out-of-pool |")
    md.append("|---|---|---|---|---|---|")
    for cs in cohort_summaries:
        md.append(
            f"| **{cs['cohort_id']}** | {cs['type']} | `{cs['agent_model']}` | "
            f"`{cs['harness_llm_pass1']}` | `{cs['harness_llm_pass2']}` | "
            f"{cs['out_of_pool_family']} |"
        )
    md.append("")

    md.append("## Score matrix (agent x cohort) — mean across P1 + P2")
    md.append("")
    header = "| Agent | " + " | ".join(cs["cohort_id"] for cs in cohort_summaries) + " | Agent mean |"
    sep = "|---|" + "|".join(["---:"] * len(cohort_summaries)) + "|---:|"
    md.append(header)
    md.append(sep)
    for spec_name in spec_names:
        row_means = []
        cells_str = []
        for cs in cohort_summaries:
            m = cell_mean(cs["cohort_id"], spec_name)
            if m is None:
                cells_str.append("ERR")
                continue
            cells_str.append(f"{m:.2f}")
            row_means.append(m)
        avg = f"{sum(row_means) / len(row_means):.2f}" if row_means else "—"
        md.append(f"| {spec_name} | " + " | ".join(cells_str) + f" | {avg} |")
    cohort_means = [f"{cs['overall']['mean']:.2f}" for cs in cohort_summaries]
    md.append("| **Cohort mean** | " + " | ".join(cohort_means) + " | |")
    md.append("")

    md.append("## Cost-floor deltas (cheap juror vs strong juror, same agent)")
    md.append("")
    if not deltas:
        md.append("_No deltas computable (one or both cohorts had no successful cells)._")
    else:
        md.append("| Comparison | Sym mean | Asym mean | Δ mean | Interpretation |")
        md.append("|---|---:|---:|---:|---|")
        for key, d in deltas.items():
            label = key.replace("_cost_floor", " agent: cheap juror vs strong juror")
            interp = (
                "✓ cost-floor preserves verdict (within 0.5 pts)"
                if abs(d["delta_mean"]) < 0.5
                else f"⚠ cheap juror over-rates by {d['delta_mean']:+.2f} pts"
                if d["delta_mean"] > 0
                else f"⚠ cheap juror under-rates by {d['delta_mean']:+.2f} pts"
            )
            md.append(
                f"| {label} | {d['sym_mean']:.2f} | {d['asym_mean']:.2f} | "
                f"{d['delta_mean']:+.2f} | {interp} |"
            )
    md.append("")

    (benchmark_dir / "benchmark_summary.md").write_text("\n".join(md))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Multi-Agent Benchmark — 4 cohorts x 4 application agents = 16 evals. "
            "Each (cohort, agent) cell uses a unique seed for fresh trap rotation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--cohorts", type=int, nargs="+", choices=[1, 2, 3, 4], default=[1, 2, 3, 4],
        help="Which cohorts to run (1=Sym-GPT5, 2=Sym-Opus, 3=Asym-GPT5, 4=Asym-Opus). Default: all 4.",
    )
    p.add_argument(
        "--agents", type=str, nargs="+",
        choices=["medical_triage_assistant", "privacy_security_agent", "code_generation_agent", "customer_support_agent"],
        default=None,
        help="Which agents to run. Default: all 4.",
    )
    p.add_argument("--base-seed", type=int, default=42, help="Base seed; per-cell seeds derived from it. Default: 42")
    p.add_argument("--turns", type=int, default=20, help="Turns per cell. Default: 20")
    p.add_argument(
        "--consensus", default="debate",
        choices=["independent", "delphi", "debate"],
        help="Consensus strategy. Default: debate (jurors debate disagreements; "
             "more rigorous when there's no consensus). Use 'independent' for "
             "cheapest/fastest with no debate rounds.",
    )
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress per-eval progress bars.")
    p.add_argument("--output-tag", default=None, help="Suffix for output folder (defaults to timestamp).")
    p.add_argument(
        "--skip-preflight", action="store_true",
        help="Skip pre-flight LLM check. Skip ONLY if you already validated all models in a recent run.",
    )
    p.add_argument(
        "--proxy-url-pass2", default=None,
        help="OpenAI-compatible proxy URL for cohorts whose P2 juror is a "
             "local model (e.g., gemma-4-E4B-it-MLX-8bit via LM Studio). "
             "Required when running Asym_GPT5 or Asym_Opus.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    console = Console()

    tag = args.output_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    benchmark_dir = RESULTS_DIR / f"multi_agent_benchmark_{tag}"
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    selected_cohorts = [COHORTS[i - 1] for i in args.cohorts]
    spec_paths = AGENT_SPEC_PATHS
    if args.agents:
        spec_paths = [p for p in AGENT_SPEC_PATHS if p.stem in args.agents]
    specs = [load_agent_spec(p) for p in spec_paths]

    console.print(Rule("Multi-Agent Benchmark", style="bold magenta"))
    console.print(
        f"\n[bold]Output folder:[/bold] {benchmark_dir.relative_to(Path.cwd())}\n"
        f"[bold]Cohorts:[/bold] {len(selected_cohorts)} of 4\n"
        f"[bold]Agents:[/bold] {len(specs)} of 4 ({', '.join(s.name for s in specs)})\n"
        f"[bold]Turns:[/bold] {args.turns}\n"
        f"[bold]Consensus:[/bold] {args.consensus}\n"
        f"[bold]Base seed:[/bold] {args.base_seed} (per-cell seeds derived)\n"
        f"[bold]Total cells:[/bold] {len(selected_cohorts) * len(specs)} "
        f"x 2 passes (P1+P2) = {len(selected_cohorts) * len(specs) * 2} evals"
    )

    design = Table(
        title="Cohort design (P1 + P2 cross-juror per cell)",
        title_style="bold cyan",
        show_header=True, header_style="bold magenta", show_lines=True,
    )
    design.add_column("#", justify="right", style="dim")
    design.add_column("Cohort", style="bold")
    design.add_column("Type")
    design.add_column("Agent LLM")
    design.add_column("P1 juror")
    design.add_column("P2 juror")
    design.add_column("Out-of-pool")
    for i, c in enumerate(selected_cohorts, 1):
        p2_label = c["harness_llm_pass2"]
        if c.get("uses_proxy_pass2"):
            p2_label = f"{c['harness_llm_pass2']} (proxy)"
        design.add_row(
            str(args.cohorts[i - 1]), c["name"], c["type"],
            c["agent_model"],
            c["harness_llm_pass1"], p2_label,
            c["out_of_pool_family"],
        )
    console.print()
    console.print(design)

    if not args.skip_preflight:
        ok, _ = preflight_check(
            selected_cohorts, specs, console,
            proxy_url_pass2=args.proxy_url_pass2,
        )
        if not ok:
            return 1

    benchmark_t0 = time.time()
    cohort_summaries: list[dict[str, Any]] = []

    for _, cohort in enumerate(selected_cohorts):
        cohort_dir = benchmark_dir / cohort["id"]
        original_idx = COHORTS.index(cohort)
        cohort_t0 = time.time()
        summary = run_cohort(
            cohort=cohort,
            cohort_idx=original_idx,
            specs=specs,
            base_seed=args.base_seed,
            turns=args.turns,
            consensus=args.consensus,
            cohort_dir=cohort_dir,
            console=console,
            verbose=not args.quiet,
            proxy_url_pass2=args.proxy_url_pass2,
        )
        cohort_summaries.append(summary)
        cohort_elapsed = time.time() - cohort_t0
        console.print(
            f"\n[dim]  Cohort '{cohort['name']}' done in {cohort_elapsed/60:.1f} min "
            f"({summary['overall']['n_ok']}/{summary['overall']['n_cells']} OK)[/dim]"
        )

    benchmark_elapsed = time.time() - benchmark_t0

    console.print(Rule("Benchmark report", style="bold magenta"))
    spec_names = [s.name for s in specs]
    render_matrix_table(cohort_summaries, spec_names, console)

    write_benchmark_summary(
        benchmark_dir, cohort_summaries, spec_names,
        base_seed=args.base_seed, turns=args.turns,
        consensus=args.consensus, runtime_s=benchmark_elapsed,
    )

    console.print()
    console.print(Panel(
        f"[bold green]Benchmark complete[/bold green] in {benchmark_elapsed/60:.1f} min\n\n"
        f"Per-cohort folders + summaries: [bold]{benchmark_dir.relative_to(Path.cwd())}/[/bold]\n"
        f"Cross-cohort matrix:            [bold]{benchmark_dir.relative_to(Path.cwd())}/benchmark_summary.md[/bold]\n",
        title="Output locations",
        border_style="green",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
