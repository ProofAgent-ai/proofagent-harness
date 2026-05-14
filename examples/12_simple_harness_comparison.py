"""Simple Harness Comparison — 4 cohorts x 4 application agents = 16 evals.

Single-juror per cohort (no P1+P2 cross-juror passes). Designed to make
two specific claims as cleanly as possible:

  Claim A: A STRONG harness LLM can challenge a STRONG agent across 4
           production-style application agents (medical triage, GDPR/CCPA
           privacy, code generation, customer support).
  Claim B: An EXTREMELY SMALL local harness LLM (Gemma 4B 8-bit, MLX) can
           ALSO challenge a strong agent across the same 4 applications,
           proving that the harness ARCHITECTURE is the key variable, not
           the size of the LLM in the juror role.

Cohort matrix (each cell = ONE eval, single juror, unique seed):

  +------------+--------------------------+----------------------------------+
  |            | Strong harness            | Weak harness (local Gemma 4B)    |
  +------------+--------------------------+----------------------------------+
  | gpt-5.5    | Strong-on-GPT5            | Weak-on-GPT5                     |
  |   agent    | juror=claude-opus-4-7     | juror=gemma-4-E4B-it-MLX-8bit    |
  +------------+--------------------------+----------------------------------+
  | opus-4-7   | Strong-on-Opus            | Weak-on-Opus                     |
  |   agent    | juror=gpt-5.5             | juror=gemma-4-E4B-it-MLX-8bit    |
  +------------+--------------------------+----------------------------------+

Cross-family rule: each cohort's juror is from a different family than the
agent (Anthropic juror for OpenAI agent, OpenAI juror for Anthropic agent,
Google juror for both via local Gemma).

Per-cell unique seed: base_seed * 1000 + cohort_idx * 100 + agent_idx
(so all 16 cells see different trap rotations).

Output structure:

    results/simple_harness_comparison_<timestamp>/
      Strong_GPT5/
        medical_triage_assistant.json
        privacy_security_agent.json
        code_generation_agent.json
        customer_support_agent.json
        cohort_summary.{json,md}
      Weak_GPT5/    (same shape)
      Strong_Opus/  (same shape)
      Weak_Opus/    (same shape)
      benchmark_summary.{json,md}

Quickstart:
    python examples/12_simple_harness_comparison.py \\
        --proxy-url http://localhost:1234/v1

Required env:
    OPENAI_API_KEY      (gpt-5.5 agent + juror in cohort 3)
    ANTHROPIC_API_KEY   (claude-opus-4-7 agent + juror in cohort 1)
    LM Studio / proxy   (gemma-4-E4B-it-MLX-8bit, used by cohorts 2 + 4)
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

from proofagent_harness import LLM, Harness

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
        "id": "Strong_GPT5",
        "name": "Strong-on-GPT5 (Opus juror, GPT-5.5 agent)",
        "agent_model": "gpt-5.5",
        "harness_llm": "claude-opus-4-7",
        "uses_proxy": False,
        "tier": "strong",
        "out_of_pool_family": "OpenAI",
        "description": "Strong cross-family juror (Opus 4.7) judging strong GPT-5.5 agent. Proves Claim A.",
    },
    {
        "id": "Weak_GPT5",
        "name": "Weak-on-GPT5 (local Gemma 4B juror, GPT-5.5 agent)",
        "agent_model": "gpt-5.5",
        "harness_llm": "gemma-4-E4B-it-MLX-8bit",
        "uses_proxy": True,
        "tier": "weak",
        "out_of_pool_family": "OpenAI",
        "description": "Extremely small (4B 8-bit) local juror judging strong GPT-5.5 agent. Proves Claim B.",
    },
    {
        "id": "Strong_Opus",
        "name": "Strong-on-Opus (GPT-5.5 juror, Opus 4.7 agent)",
        "agent_model": "claude-opus-4-7",
        "harness_llm": "gpt-5.5",
        "uses_proxy": False,
        "tier": "strong",
        "out_of_pool_family": "Anthropic",
        "description": "Strong cross-family juror (GPT-5.5) judging strong Opus 4.7 agent. Confirms Claim A on second frontier agent.",
    },
    {
        "id": "Weak_Opus",
        "name": "Weak-on-Opus (local Gemma 4B juror, Opus 4.7 agent)",
        "agent_model": "claude-opus-4-7",
        "harness_llm": "gemma-4-E4B-it-MLX-8bit",
        "uses_proxy": True,
        "tier": "weak",
        "out_of_pool_family": "Anthropic",
        "description": "Extremely small (4B 8-bit) local juror judging strong Opus 4.7 agent. Confirms Claim B on second frontier agent.",
    },
]

AGENT_SPEC_PATHS: list[Path] = [
    AGENTS_DIR / "medical_triage_assistant.json",
    AGENTS_DIR / "privacy_security_agent.json",
    AGENTS_DIR / "code_generation_agent.json",
    AGENTS_DIR / "customer_support_agent.json",
]


# Paper-grade LLM descriptors (used in per-cohort design docs)
LLM_PROFILES: dict[str, dict[str, Any]] = {
    "gpt-5.5": {
        "family": "OpenAI",
        "tier": "frontier",
        "release": "April 2026",
        "context_window": "256K tokens",
        "description": (
            "OpenAI's flagship reasoning-augmented model (April 2026 release). "
            "Top-tier general-purpose capability with extended internal "
            "reasoning. Used for both adversarial agent role and cross-family "
            "juror role in this benchmark."
        ),
    },
    "claude-opus-4-7": {
        "family": "Anthropic",
        "tier": "frontier",
        "release": "Q1 2026",
        "context_window": "1M tokens",
        "description": (
            "Anthropic's flagship reasoning model. Top-tier general-purpose "
            "capability with extended thinking. Used for both adversarial "
            "agent role and cross-family juror role in this benchmark."
        ),
    },
    "gemma-4-E4B-it-MLX-8bit": {
        "family": "Google (open-weights, locally hosted)",
        "tier": "small / cost-floor",
        "release": "2026 (community 8-bit MLX quant)",
        "parameters": "4B",
        "quantization": "8-bit MLX (Apple Silicon)",
        "context_window": "8K tokens (typical local config)",
        "description": (
            "4-billion-parameter quantized open-weights instruction-tuned "
            "model running locally via LM Studio (Apple Silicon MLX backend). "
            "Used as the 'extremely small' juror to test whether the harness "
            "ARCHITECTURE can extract useful adversarial verdicts even from "
            "a small local model — proving the harness scaffolding is the "
            "key variable, not the LLM size."
        ),
    },
}


def _llm_profile_md(model: str) -> str:
    """Render an LLM profile as a markdown blockquote, with sensible
    defaults if the model isn't in LLM_PROFILES."""
    p = LLM_PROFILES.get(model, {})
    if not p:
        return f"> `{model}` — (profile not registered)"
    lines = [f"- **Model:** `{model}`"]
    for k in ("family", "tier", "release", "parameters", "quantization", "context_window"):
        if k in p:
            lines.append(f"- **{k.replace('_', ' ').title()}:** {p[k]}")
    if p.get("description"):
        lines.append(f"- **Description:** {p['description']}")
    return "\n".join(lines)


# Fallback chains for transient unavailability (connection / 5xx / overloaded).
# Pre-populated with substring-match for known restricted families.
MODEL_FALLBACK_CHAINS: dict[str, list[str]] = {
    "gpt-5.5": ["gpt-5.4", "gpt-4.1"],
    "claude-opus-4-7": ["anthropic/claude-sonnet-4-6", "gpt-4.1"],
    "gemma-4-E4B-it-MLX-8bit": ["anthropic/claude-haiku-4-5", "gpt-4o-mini"],
}


def _derive_seed(base_seed: int, cohort_idx: int, agent_idx: int) -> int:
    """Each (cohort, agent) cell gets a unique reproducible seed so all 16
    cells see different trap rotations; whole benchmark replayable from
    --base-seed."""
    return base_seed * 1000 + cohort_idx * 100 + agent_idx


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: ping each LLM with a meaningful prompt + display the response
# ─────────────────────────────────────────────────────────────────────────────

PREFLIGHT_PROMPT = (
    "Briefly identify your model name in one sentence, then confirm you are "
    "operational and ready to perform structured evaluation tasks."
)


def _required_envs(models: list[str]) -> set[str]:
    needed: set[str] = set()
    for m in models:
        ml = m.lower()
        if "claude" in ml or "anthropic" in ml:
            needed.add("ANTHROPIC_API_KEY")
        if "gpt" in ml or "openai/" in ml or ml.startswith("gpt-"):
            needed.add("OPENAI_API_KEY")
        # Gemma local doesn't need an env var (uses proxy URL)
    return needed


def _ping_litellm(model: str, prompt: str, *, timeout: int = 60) -> str:
    """Send a substantive prompt to the model and return its response. Handles
    GPT-5.x reasoning / Opus 4.7 thinking-tier param differences and gives
    generous output budget so reasoning tokens don't crowd out visible text.
    `timeout` is generous-by-default; pass 300+ for local proxied models that
    need cold-start warmup time."""
    import litellm

    ml = model.lower()
    is_reasoning = any(s in ml for s in ("gpt-5", "o1-", "o3-", "o4-"))
    is_thinking = any(s in ml for s in ("claude-opus-4-7", "claude-opus-4-8"))

    base_kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "timeout": timeout,
    }
    if is_reasoning:
        base_kwargs["max_completion_tokens"] = 512
    else:
        base_kwargs["max_tokens"] = 512 if is_thinking else 256
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
            base_kwargs["max_completion_tokens"] = max(512, base_kwargs.pop("max_tokens", 256))
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
    proxy_url: str | None = None,
) -> tuple[bool, list[str]]:
    """Ping every unique LLM with a meaningful prompt and DISPLAY the response,
    then verify each agent constructor. Aborts before any cohort runs if any
    check fails. Saves hours of wasted compute."""
    console.print()
    console.print(Rule("Pre-flight LLM check (display sample responses)", style="bold yellow"))

    # 1) env-var presence check (only API models — Gemma local uses proxy URL)
    api_models = [
        m for c in cohorts
        for m in (c["agent_model"], c["harness_llm"])
        if not c.get("uses_proxy", False) or m == c["agent_model"]
    ]
    needed = _required_envs(api_models)
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

    # 2) collect unique (model, role, is_proxied)
    unique_pings: set[tuple[str, str, bool]] = set()
    for c in cohorts:
        unique_pings.add((c["agent_model"], "agent", False))
        unique_pings.add((c["harness_llm"], "harness", c.get("uses_proxy", False)))

    console.print(
        f"\n[dim]Pinging {len(unique_pings)} unique model/role combinations with "
        f"a meaningful prompt (60s timeout for API models, 300s for local "
        f"proxied models that may need cold-start warmup):[/dim]"
    )
    console.print(f"[dim]  prompt: {PREFLIGHT_PROMPT!r}[/dim]\n")

    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")

    errors: list[str] = []
    response_table = Table(
        title="LLM responses (verify each before cohort runs start)",
        title_style="bold cyan",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    response_table.add_column("Model", style="bold", no_wrap=False)
    response_table.add_column("Role", no_wrap=True)
    response_table.add_column("Response (first 400 chars)", no_wrap=False)
    response_table.add_column("Status", justify="center")

    for model, role, is_proxied in sorted(unique_pings):
        target_model = model
        display_name = model
        if is_proxied:
            if not proxy_url:
                err = f"  {model} ({role}): --proxy-url required but not provided"
                errors.append(err)
                response_table.add_row(model, role, "—", Text("✗ no proxy URL", style="red"))
                continue
            target_model = _wire_proxy_for_harness_llm(proxy_url, model)
            display_name = f"{model}\n[dim](via proxy)[/dim]"
        ping_timeout = 300 if is_proxied else 60
        try:
            content = _ping_litellm(target_model, PREFLIGHT_PROMPT, timeout=ping_timeout)
            if not content:
                err = f"  {target_model} ({role}): empty response"
                errors.append(err)
                response_table.add_row(
                    display_name, role, "[dim]<empty>[/dim]",
                    Text("✗ empty", style="red"),
                )
            else:
                snippet = content[:400] + ("..." if len(content) > 400 else "")
                response_table.add_row(
                    display_name, role, snippet,
                    Text("✓", style="bold green"),
                )
        except Exception as exc:
            short = str(exc)[:200].replace("\n", " ")
            errors.append(f"  {target_model} ({role}): {type(exc).__name__}: {short}")
            response_table.add_row(
                display_name, role, f"[red]{type(exc).__name__}: {short[:200]}[/red]",
                Text("✗", style="bold red"),
            )
        finally:
            if is_proxied:
                _restore_proxy_env(saved_base, saved_key)

    console.print(response_table)

    # 3) agent constructor check
    console.print()
    console.print("[dim]Verifying agent constructor for each unique (agent_model x agent_spec)...[/dim]")
    agent_table = Table(
        show_header=True, header_style="bold magenta",
    )
    agent_table.add_column("Agent model", style="bold")
    agent_table.add_column("Application")
    agent_table.add_column("Response (first 200 chars)")
    agent_table.add_column("Status", justify="center")

    unique_agents: set[tuple[str, str]] = set()
    for c in cohorts:
        for spec in specs:
            unique_agents.add((c["agent_model"], spec.name))
    for agent_model, spec_name in sorted(unique_agents):
        spec = next(s for s in specs if s.name == spec_name)
        try:
            agent = make_agent_from_spec(spec, agent_model)
            r = agent("Briefly confirm you are operational and aware of your role.")
            text = (r.text or "").strip()
            if not text:
                errors.append(f"  agent({agent_model}, {spec_name}): empty response")
                agent_table.add_row(
                    agent_model, spec_name, "[dim]<empty>[/dim]",
                    Text("✗", style="red"),
                )
            else:
                snippet = text[:200] + ("..." if len(text) > 200 else "")
                agent_table.add_row(agent_model, spec_name, snippet, Text("✓", style="bold green"))
        except Exception as exc:
            short = str(exc)[:200].replace("\n", " ")
            errors.append(f"  agent({agent_model}, {spec_name}): {type(exc).__name__}: {short}")
            agent_table.add_row(
                agent_model, spec_name, f"[red]{type(exc).__name__}: {short[:120]}[/red]",
                Text("✗", style="red"),
            )

    console.print(agent_table)

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
    console.print(
        "[bold green]✓ All pre-flight checks passed — review the responses above, "
        "then proceed with cohort evals[/bold green]"
    )
    return True, []


# ─────────────────────────────────────────────────────────────────────────────
# Per-cohort design document (paper-grade)
# ─────────────────────────────────────────────────────────────────────────────

def write_cohort_design(
    cohort: dict[str, Any],
    specs: list[AgentSpec],
    cohort_dir: Path,
    *,
    base_seed: int,
    turns: int,
    consensus: str,
) -> None:
    """Write cohort_design.{md,json} into the cohort folder BEFORE evals run.
    Captures full experimental context for paper-grade reproducibility:
    harness LLM profile, agent LLM profile, every application's role / system
    prompt / skills / guardrails / tools / knowledge / business case / goal.
    Saved up-front so the experimental setup is documented even if some
    cells fail."""
    cohort_dir.mkdir(parents=True, exist_ok=True)

    # Structured JSON view (machine-readable)
    design = {
        "cohort_id": cohort["id"],
        "cohort_name": cohort["name"],
        "tier": cohort["tier"],
        "uses_proxy": cohort.get("uses_proxy", False),
        "out_of_pool_family": cohort["out_of_pool_family"],
        "description": cohort["description"],
        "experimental_setup": {
            "turns_per_eval": turns,
            "consensus_strategy": consensus,
            "base_seed": base_seed,
            "n_application_agents": len(specs),
            "evals_in_cohort": len(specs),
            "single_juror_per_cell": True,
            "cross_family_rule": (
                f"Agent's family ({cohort['out_of_pool_family']}) is excluded "
                f"from the juror pool by design."
            ),
        },
        "harness_llm": {
            "model": cohort["harness_llm"],
            "role": (
                "Multi-juror scoring — 3 calibrated personas (rigorous, "
                "lenient, contrarian) with debate consensus across the 5 "
                "canonical metrics (task_success, hallucination_resistance, "
                "safety, instruction_following, manipulation_resistance)."
            ),
            **LLM_PROFILES.get(cohort["harness_llm"], {}),
        },
        "agent_llm": {
            "model": cohort["agent_model"],
            "role": (
                "Underlying LLM that powers each application agent. The same "
                "agent LLM is wrapped with each application's specific system "
                "prompt, tools, and knowledge before being driven through "
                f"{turns} adversarial turns by the harness's conductor."
            ),
            **LLM_PROFILES.get(cohort["agent_model"], {}),
        },
        "application_agents": [
            {
                "name": s.name,
                "role": s.role,
                "domain": s.domain,
                "business_case": s.business_case,
                "goal": s.goal,
                "skills": s.skills,
                "guardrails": s.guardrails,
                "n_tools": len(s.tools_openai),
                "tools": [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                    }
                    for t in s.tools_openai
                ],
                "knowledge_excerpt": s.knowledge,
                "system_prompt": s.system_prompt,
            }
            for s in specs
        ],
    }
    (cohort_dir / "cohort_design.json").write_text(
        json.dumps(design, indent=2, default=str)
    )

    # Human-readable markdown (paper-friendly)
    md: list[str] = []
    md.append(f"# Cohort: {cohort['name']}")
    md.append("")
    md.append(f"_{cohort['description']}_")
    md.append("")
    md.append("## Experimental Setup")
    md.append("")
    md.append(f"- **Cohort ID:** `{cohort['id']}`")
    md.append(f"- **Tier:** `{cohort['tier']}`")
    md.append(f"- **Out-of-pool family (cross-family rule):** `{cohort['out_of_pool_family']}`")
    md.append(f"- **Turns per evaluation:** {turns}")
    md.append(f"- **Consensus strategy:** `{consensus}` (3 juror personas)")
    md.append(f"- **Base seed:** {base_seed} (per-cell seeds derived)")
    md.append(f"- **Application agents in this cohort:** {len(specs)}")
    md.append("- **Single juror per cell:** Yes (no P1+P2 cross-juror passes)")
    md.append("")
    md.append("## Harness LLM (Juror)")
    md.append("")
    md.append(_llm_profile_md(cohort["harness_llm"]))
    md.append("")
    md.append(
        "**Role in this cohort:** Multi-juror scoring with 3 calibrated "
        "personas (rigorous, lenient, contrarian) running the configured "
        "consensus strategy across the 5 canonical metrics:"
    )
    md.append("")
    md.append("- `task_success`")
    md.append("- `hallucination_resistance`")
    md.append("- `safety`")
    md.append("- `instruction_following`")
    md.append("- `manipulation_resistance`")
    md.append("")
    md.append("## Agent LLM (Under Test)")
    md.append("")
    md.append(_llm_profile_md(cohort["agent_model"]))
    md.append("")
    md.append(
        f"**Role in this cohort:** This LLM is wrapped with each application "
        f"agent's specific system prompt, tool surface, and knowledge "
        f"document, then driven through {turns} adversarial turns by the "
        f"harness's conductor."
    )
    md.append("")
    md.append("## Application Agents Under Test")
    md.append("")
    md.append(
        f"This cohort runs **{len(specs)} application agents** back-to-back, "
        "each evaluated independently. Each agent application defines its "
        "own scope, tools, knowledge, and guardrails — all of which the "
        "underlying agent LLM must respect under adversarial pressure."
    )
    md.append("")
    for i, s in enumerate(specs, 1):
        md.append(f"### {i}. {s.name}")
        md.append("")
        md.append(f"- **Role:** {s.role}")
        md.append(f"- **Domain:** `{s.domain}`")
        md.append(f"- **Business case:** {s.business_case}")
        md.append(f"- **Goal:** {s.goal}")
        md.append("")
        md.append("#### Skills")
        md.append("")
        for sk in s.skills:
            md.append(f"- {sk}")
        md.append("")
        md.append("#### Guardrails")
        md.append("")
        for g in s.guardrails:
            md.append(f"- {g}")
        md.append("")
        md.append(f"#### Tools ({len(s.tools_openai)})")
        md.append("")
        md.append("| # | Tool name | Description |")
        md.append("|---|---|---|")
        for j, t in enumerate(s.tools_openai, 1):
            tname = t["function"]["name"]
            tdesc = t["function"]["description"].replace("|", "\\|").replace("\n", " ")
            md.append(f"| {j} | `{tname}` | {tdesc} |")
        md.append("")
        if s.knowledge:
            md.append("#### External Knowledge")
            md.append("")
            md.append("> " + s.knowledge.replace("\n", "\n> "))
            md.append("")
        md.append("#### System Prompt")
        md.append("")
        md.append("```")
        md.append(s.system_prompt)
        md.append("```")
        md.append("")
    (cohort_dir / "cohort_design.md").write_text("\n".join(md))


# ─────────────────────────────────────────────────────────────────────────────
# Per-cell evaluation (single juror, no P1+P2)
# ─────────────────────────────────────────────────────────────────────────────

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
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Run one (cohort, agent) cell as a single eval. Returns the result dict."""
    cell_dir.mkdir(parents=True, exist_ok=True)
    output_path = cell_dir / f"{spec.name}.json"

    # Wire proxy if cohort uses local model
    harness_llm = cohort["harness_llm"]
    is_proxied = cohort.get("uses_proxy", False)
    saved_base = os.environ.get("OPENAI_BASE_URL")
    saved_key = os.environ.get("OPENAI_API_KEY")
    if is_proxied:
        if not proxy_url:
            raise SystemExit(
                f"Cohort '{cohort['name']}' requires --proxy-url "
                f"(harness LLM = {harness_llm})."
            )
        harness_llm = _wire_proxy_for_harness_llm(proxy_url, cohort["harness_llm"])

    console.print()
    console.print(
        f"[bold cyan]▶[/bold cyan] {cohort['id']} x {spec.name}  "
        f"agent={cohort['agent_model']}  juror={harness_llm}  "
        f"seed={seed}  turns={turns}  consensus={consensus}"
    )

    t0 = time.time()
    try:
        agent_callable = make_agent_from_spec(spec, cohort["agent_model"])
        agent_context = make_context_from_spec(spec)
        # For proxied (local) jurors, construct an LLM instance with a generous
        # per-call timeout so juror scoring of long 25-turn transcripts on a
        # slow local 4B model doesn't time out mid-eval.
        harness_llm_arg: str | LLM = harness_llm
        if is_proxied:
            harness_llm_arg = LLM(
                model=harness_llm,
                seed=seed,
                extra_kwargs={"timeout": 900},  # 15 min per juror call
            )
        report = Harness(
            llm=harness_llm_arg,
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
            f"  [red]✗ FAILED[/red] after {elapsed:.0f}s — "
            f"{type(exc).__name__}: {exc}"
        )
        if is_proxied:
            _restore_proxy_env(saved_base, saved_key)
        return {
            "cohort_id": cohort["id"],
            "agent_name": spec.name,
            "agent_domain": spec.domain,
            "agent_model": cohort["agent_model"],
            "harness_llm": cohort["harness_llm"],
            "seed": seed,
            "turns": turns,
            "error": f"{type(exc).__name__}: {exc}",
            "duration": elapsed,
        }
    finally:
        if is_proxied:
            _restore_proxy_env(saved_base, saved_key)

    elapsed = time.time() - t0
    report.to_json(str(output_path))

    result = {
        "cohort_id": cohort["id"],
        "agent_name": spec.name,
        "agent_domain": spec.domain,
        "agent_model": cohort["agent_model"],
        "harness_llm": cohort["harness_llm"],
        "tier": cohort["tier"],
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
        f"  [green]✓[/green] {result['final_score']:.2f}  "
        f"[{cert_color}]{result['certification']}[/{cert_color}]  "
        f"({elapsed:.0f}s, {result['tokens']:,} tokens, {result['n_findings']} findings)"
    )
    return result


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
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Run one cohort across all agent specs. Returns aggregated dict."""
    cohort_dir.mkdir(parents=True, exist_ok=True)
    cell_results: list[dict[str, Any]] = []

    # Write the paper-grade design doc up-front (saved even if some cells fail)
    write_cohort_design(
        cohort, specs, cohort_dir,
        base_seed=base_seed, turns=turns, consensus=consensus,
    )

    console.print()
    console.print(Rule(f"  ▶ COHORT — {cohort['name']}  ", style="bold green"))
    console.print(f"[dim]  {cohort['description']}[/dim]")
    console.print(f"[dim]  output: {cohort_dir.relative_to(Path.cwd())}[/dim]")
    console.print(f"[dim]  design: {cohort_dir.relative_to(Path.cwd())}/cohort_design.md[/dim]")

    for agent_idx, spec in enumerate(specs):
        seed = _derive_seed(base_seed, cohort_idx, agent_idx)
        console.print()
        console.print(
            Rule(
                f"  CELL {agent_idx + 1}/{len(specs)} — {cohort['id']} x {spec.name}  ",
                style="bold blue",
            )
        )
        cell_results.append(run_one_cell(
            cohort=cohort,
            spec=spec,
            seed=seed,
            turns=turns,
            consensus=consensus,
            cell_dir=cohort_dir,
            console=console,
            verbose=verbose,
            proxy_url=proxy_url,
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
        "tier": cohort["tier"],
        "agent_model": cohort["agent_model"],
        "harness_llm": cohort["harness_llm"],
        "uses_proxy": cohort.get("uses_proxy", False),
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
        f"**ID:** `{cohort['id']}`  |  **Tier:** `{cohort['tier']}`  |  "
        f"**Out-of-pool family:** `{cohort['out_of_pool_family']}`",
        f"**Agent model:** `{cohort['agent_model']}`  |  "
        f"**Harness juror:** `{cohort['harness_llm']}`"
        + (" *(proxy)*" if cohort.get("uses_proxy") else ""),
        f"**Turns:** {turns}  |  **Consensus:** `{consensus}`",
        "",
        f"_{cohort['description']}_",
        "",
        "## Per-agent results",
        "",
        "| Agent | Domain | Score | Cert | Findings | Tokens | Duration | Seed |",
        "|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for c in cell_results:
        if "error" in c:
            md_lines.append(
                f"| {c['agent_name']} | — | ERROR | — | — | — | "
                f"{c['duration']:.0f}s | {c['seed']} |"
            )
        else:
            md_lines.append(
                f"| {c['agent_name']} | {c['agent_domain']} | "
                f"{c['final_score']:.2f} | {c['certification']} | "
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
# Cross-cohort summary (4-cohort x 4-agent matrix + harness-tier deltas)
# ─────────────────────────────────────────────────────────────────────────────

def render_matrix_table(
    cohort_summaries: list[dict[str, Any]],
    spec_names: list[str],
    console: Console,
) -> None:
    """Render the 4-cohort x 4-agent score matrix as a Rich table."""
    table = Table(
        title="4-cohort x 4-agent score matrix (harness tier comparison)",
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

    by_cohort_agent: dict[tuple[str, str], dict[str, Any]] = {}
    for cs in cohort_summaries:
        for cell in cs["cells"]:
            by_cohort_agent[(cs["cohort_id"], cell["agent_name"])] = cell

    for spec_name in spec_names:
        row = [spec_name]
        for cs in cohort_summaries:
            cell = by_cohort_agent.get((cs["cohort_id"], spec_name))
            if cell is None or "error" in cell:
                row.append(Text("ERR", style="red"))
                continue
            score = cell["final_score"]
            cert = cell["certification"]
            color = {
                "GOLD": "yellow",
                "SILVER": "bright_white",
                "NEEDS_ENHANCEMENT": "yellow3",
                "NOT_READY": "red",
            }.get(cert, "white")
            row.append(Text(f"{score:.2f} {cert[:4]}", style=color))
        table.add_row(*row)

    means_row = [Text("Cohort mean", style="bold dim")]
    for cs in cohort_summaries:
        means_row.append(Text(f"{cs['overall']['mean']:.2f}", style="bold"))
    table.add_row(*means_row)

    console.print()
    console.print(table)


def render_harness_tier_deltas(
    cohort_summaries: list[dict[str, Any]],
    spec_names: list[str],
    console: Console,
) -> dict[str, Any]:
    """Compute Strong-vs-Weak harness deltas per agent LLM (the headline result)."""
    by_id = {cs["cohort_id"]: cs for cs in cohort_summaries}
    by_cohort_agent: dict[tuple[str, str], dict[str, Any]] = {}
    for cs in cohort_summaries:
        for cell in cs["cells"]:
            by_cohort_agent[(cs["cohort_id"], cell["agent_name"])] = cell

    table = Table(
        title="Harness tier deltas (Strong vs Weak juror, same agent)",
        title_style="bold cyan",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Agent LLM", style="bold")
    table.add_column("Application")
    table.add_column("Strong (Opus/GPT-5.5)", justify="right")
    table.add_column("Weak (Gemma 4B)", justify="right")
    table.add_column("Δ (Weak - Strong)", justify="right")
    table.add_column("Interpretation", style="dim")

    deltas: dict[str, Any] = {"per_agent_per_app": {}, "per_agent_mean": {}}

    pairs = [
        ("gpt-5.5", "Strong_GPT5", "Weak_GPT5"),
        ("claude-opus-4-7", "Strong_Opus", "Weak_Opus"),
    ]
    for agent_model, strong_id, weak_id in pairs:
        strong_cohort = by_id.get(strong_id)
        weak_cohort = by_id.get(weak_id)
        if strong_cohort is None or weak_cohort is None:
            continue
        strong_scores: list[float] = []
        weak_scores: list[float] = []
        for spec_name in spec_names:
            s_cell = by_cohort_agent.get((strong_id, spec_name))
            w_cell = by_cohort_agent.get((weak_id, spec_name))
            if s_cell is None or w_cell is None or "error" in s_cell or "error" in w_cell:
                continue
            s_score = s_cell["final_score"]
            w_score = w_cell["final_score"]
            delta = w_score - s_score
            interp = (
                "✓ same verdict (Δ < 0.5)" if abs(delta) < 0.5
                else f"⚠ weak over-rates by {delta:+.2f}" if delta > 0
                else f"✓ weak harness MORE strict by {delta:+.2f}"
            )
            table.add_row(
                agent_model, spec_name,
                f"{s_score:.2f}", f"{w_score:.2f}",
                Text(f"{delta:+.2f}", style="yellow" if abs(delta) >= 0.5 else "green"),
                interp,
            )
            strong_scores.append(s_score)
            weak_scores.append(w_score)
            deltas["per_agent_per_app"][f"{agent_model}|{spec_name}"] = {
                "strong": s_score, "weak": w_score, "delta": delta,
            }
        if strong_scores and weak_scores:
            s_mean = sum(strong_scores) / len(strong_scores)
            w_mean = sum(weak_scores) / len(weak_scores)
            d_mean = w_mean - s_mean
            deltas["per_agent_mean"][agent_model] = {
                "strong_mean": s_mean, "weak_mean": w_mean, "delta_mean": d_mean,
            }
            table.add_row(
                Text(f"{agent_model} (mean)", style="bold"), Text("(across apps)", style="dim"),
                Text(f"{s_mean:.2f}", style="bold"),
                Text(f"{w_mean:.2f}", style="bold"),
                Text(f"{d_mean:+.2f}", style="bold yellow" if abs(d_mean) >= 0.5 else "bold green"),
                "",
            )

    console.print()
    console.print(table)
    return deltas


def write_benchmark_summary(
    benchmark_dir: Path,
    cohort_summaries: list[dict[str, Any]],
    spec_names: list[str],
    deltas: dict[str, Any],
    *,
    base_seed: int,
    turns: int,
    consensus: str,
    runtime_s: float,
) -> None:
    """Write benchmark_summary.{json,md} with the matrix view + harness tier deltas."""
    by_cohort_agent: dict[tuple[str, str], dict[str, Any]] = {}
    for cs in cohort_summaries:
        for cell in cs["cells"]:
            by_cohort_agent[(cs["cohort_id"], cell["agent_name"])] = cell

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
        "cohorts": [{k: v for k, v in cs.items() if k != "cells"} for cs in cohort_summaries],
        "matrix": {
            cs["cohort_id"]: {
                spec_name: by_cohort_agent.get((cs["cohort_id"], spec_name), {})
                for spec_name in spec_names
            }
            for cs in cohort_summaries
        },
        "harness_tier_deltas": deltas,
    }
    (benchmark_dir / "benchmark_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    md = [
        "# Simple Harness Comparison — Summary",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        f"**Config:** {len(cohort_summaries)} cohorts x {len(spec_names)} agents = "
        f"{len(cohort_summaries) * len(spec_names)} cells, {turns} turns/cell, "
        f"base_seed={base_seed}, consensus=`{consensus}`.",
        f"**Runtime:** {runtime_s/60:.1f} min",
        "",
        "## Cohort design",
        "",
        "| Cohort | Tier | Agent | Harness juror | Out-of-pool |",
        "|---|---|---|---|---|",
    ]
    for cs in cohort_summaries:
        proxy_note = " *(local proxy)*" if cs.get("uses_proxy") else ""
        md.append(
            f"| **{cs['cohort_id']}** | `{cs['tier']}` | `{cs['agent_model']}` | "
            f"`{cs['harness_llm']}`{proxy_note} | {cs['out_of_pool_family']} |"
        )
    md.append("")

    md.append("## Score matrix (agent x cohort)")
    md.append("")
    header = "| Agent | " + " | ".join(cs["cohort_id"] for cs in cohort_summaries) + " |"
    sep = "|---|" + "|".join(["---:"] * len(cohort_summaries)) + "|"
    md.append(header)
    md.append(sep)
    for spec_name in spec_names:
        cells_str = []
        for cs in cohort_summaries:
            cell = by_cohort_agent.get((cs["cohort_id"], spec_name))
            if cell is None or "error" in cell:
                cells_str.append("ERR")
            else:
                cells_str.append(f"{cell['final_score']:.2f} {cell['certification'][:4]}")
        md.append(f"| {spec_name} | " + " | ".join(cells_str) + " |")
    cohort_means = [f"{cs['overall']['mean']:.2f}" for cs in cohort_summaries]
    md.append("| **Cohort mean** | " + " | ".join(cohort_means) + " |")
    md.append("")

    md.append("## Harness tier deltas (Weak juror score - Strong juror score, same agent)")
    md.append("")
    if not deltas.get("per_agent_per_app"):
        md.append("_No deltas computable (one or both cohorts had no successful cells)._")
    else:
        md.append("| Agent LLM | Application | Strong | Weak | Δ |")
        md.append("|---|---|---:|---:|---:|")
        for key, d in deltas["per_agent_per_app"].items():
            agent_model, spec_name = key.split("|", 1)
            md.append(
                f"| `{agent_model}` | {spec_name} | {d['strong']:.2f} | "
                f"{d['weak']:.2f} | {d['delta']:+.2f} |"
            )
        md.append("")
        md.append("### Per-agent mean (across applications)")
        md.append("")
        md.append("| Agent LLM | Strong mean | Weak mean | Δ mean | Interpretation |")
        md.append("|---|---:|---:|---:|---|")
        for agent_model, d in deltas["per_agent_mean"].items():
            interp = (
                "Weak harness tracks strong (verdict consistent)"
                if abs(d["delta_mean"]) < 0.5
                else f"Weak harness over-rates by {d['delta_mean']:+.2f} pts"
                if d["delta_mean"] > 0
                else f"Weak harness MORE strict by {d['delta_mean']:+.2f} pts"
            )
            md.append(
                f"| `{agent_model}` | {d['strong_mean']:.2f} | {d['weak_mean']:.2f} | "
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
            "Simple Harness Comparison — 4 cohorts (Strong/Weak harness x "
            "GPT-5.5/Opus agent) x 4 application agents = 16 single-juror evals. "
            "Tests the claim that the harness ARCHITECTURE (not the LLM size) "
            "is the key variable for adversarial evaluation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--cohorts", type=int, nargs="+", choices=[1, 2, 3, 4], default=[1, 2, 3, 4],
        help="Which cohorts to run (1=Strong-GPT5, 2=Weak-GPT5, 3=Strong-Opus, 4=Weak-Opus). Default: all 4.",
    )
    p.add_argument(
        "--agents", type=str, nargs="+",
        choices=["medical_triage_assistant", "privacy_security_agent", "code_generation_agent", "customer_support_agent"],
        default=None,
        help="Which agents to run. Default: all 4.",
    )
    p.add_argument("--base-seed", type=int, default=42, help="Base seed; per-cell seeds derived from it. Default: 42")
    p.add_argument("--turns", type=int, default=25, help="Turns per cell. Default: 25")
    p.add_argument(
        "--consensus", default="debate",
        choices=["independent", "delphi", "debate"],
        help="Consensus strategy. Default: debate (juror personas debate disagreements).",
    )
    p.add_argument(
        "--proxy-url", default=None,
        help="OpenAI-compatible proxy URL for the local Gemma juror "
             "(used by Weak_GPT5 + Weak_Opus cohorts). Required if those cohorts are selected.",
    )
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress per-eval progress bars.")
    p.add_argument("--output-tag", default=None, help="Suffix for output folder (defaults to timestamp).")
    p.add_argument(
        "--skip-preflight", action="store_true",
        help="Skip pre-flight LLM check. Skip ONLY if you already validated all models in a recent run.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    console = Console()

    tag = args.output_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    benchmark_dir = RESULTS_DIR / f"simple_harness_comparison_{tag}"
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    selected_cohorts = [COHORTS[i - 1] for i in args.cohorts]
    spec_paths = AGENT_SPEC_PATHS
    if args.agents:
        spec_paths = [p for p in AGENT_SPEC_PATHS if p.stem in args.agents]
    specs = [load_agent_spec(p) for p in spec_paths]

    console.print(Rule("Simple Harness Comparison — Strong vs Weak Juror", style="bold magenta"))
    console.print(
        f"\n[bold]Output folder:[/bold] {benchmark_dir.relative_to(Path.cwd())}\n"
        f"[bold]Cohorts:[/bold] {len(selected_cohorts)} of 4\n"
        f"[bold]Agents:[/bold] {len(specs)} of 4 ({', '.join(s.name for s in specs)})\n"
        f"[bold]Turns:[/bold] {args.turns}\n"
        f"[bold]Consensus:[/bold] {args.consensus}\n"
        f"[bold]Base seed:[/bold] {args.base_seed} (per-cell seeds derived)\n"
        f"[bold]Total cells:[/bold] {len(selected_cohorts) * len(specs)} "
        f"single-juror evals"
    )

    design = Table(
        title="Cohort design", title_style="bold cyan",
        show_header=True, header_style="bold magenta", show_lines=True,
    )
    design.add_column("#", justify="right", style="dim")
    design.add_column("Cohort", style="bold")
    design.add_column("Tier")
    design.add_column("Agent LLM")
    design.add_column("Harness juror")
    design.add_column("Out-of-pool")
    for i, c in enumerate(selected_cohorts, 1):
        juror_label = c["harness_llm"]
        if c.get("uses_proxy"):
            juror_label = f"{c['harness_llm']} (proxy)"
        design.add_row(
            str(args.cohorts[i - 1]), c["name"], c["tier"],
            c["agent_model"], juror_label, c["out_of_pool_family"],
        )
    console.print()
    console.print(design)

    if not args.skip_preflight:
        ok, _ = preflight_check(
            selected_cohorts, specs, console, proxy_url=args.proxy_url,
        )
        if not ok:
            return 1

    benchmark_t0 = time.time()
    cohort_summaries: list[dict[str, Any]] = []

    for cohort in selected_cohorts:
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
            proxy_url=args.proxy_url,
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
    deltas = render_harness_tier_deltas(cohort_summaries, spec_names, console)

    write_benchmark_summary(
        benchmark_dir, cohort_summaries, spec_names, deltas,
        base_seed=args.base_seed, turns=args.turns,
        consensus=args.consensus, runtime_s=benchmark_elapsed,
    )

    console.print()
    console.print(Panel(
        f"[bold green]Benchmark complete[/bold green] in {benchmark_elapsed/60:.1f} min\n\n"
        f"Per-cohort folders + summaries: [bold]{benchmark_dir.relative_to(Path.cwd())}/[/bold]\n"
        f"Cross-cohort matrix + tier deltas: [bold]{benchmark_dir.relative_to(Path.cwd())}/benchmark_summary.md[/bold]\n",
        title="Output locations",
        border_style="green",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
