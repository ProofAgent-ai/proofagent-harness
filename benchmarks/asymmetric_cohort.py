"""Asymmetric single-cell evaluation — small local Harness LLM vs. frontier agent.

Runs ONE evaluation cell: a small/quantized Harness LLM (e.g., Gemma 4B 8-bit
served from a local proxy like LM Studio, vLLM, or Ollama) evaluating a
production-style domain agent powered by a frontier LLM (e.g., GPT-5.5,
Opus 4.7). This is the asymmetric weak-on-frontier regime studied in the paper
and reproduces the headline cohort cells.

Use this when you want to:
  - Reproduce the asymmetric replication described in the paper at 25 turns.
  - Sweep agent-LLM choices (gpt-5.5, gpt-4.1, anthropic/claude-opus-4-7, ...)
    against a fixed Harness LLM.
  - Sweep agents (customer_support, medical_triage, code_generation,
    privacy_security) against a fixed Harness LLM.
  - Evaluate your own agent spec (drop a .json into examples/agents/ and pass
    its name to --agent).

What it does
------------
The script:
  1. Loads the chosen agent JSON spec from examples/agents/.
  2. Wires the agent under test to its LLM provider (OpenAI / Anthropic /
     Gemini auto-detected from the model name).
  3. (Optional) Wires the Harness LLM to a local OpenAI-compatible proxy when
     --proxy-url is given (LM Studio / vLLM / Ollama / mlx-lm).
  4. Runs the full ProofAgent Harness pipeline: planner → conductor →
     multi-turn pressure → juror panel → consensus → evidence-linked report.
  5. Writes JSON + Markdown reports under ./results/.

Reproducing the paper's cohort cells
------------------------------------
The paper reports four agents x two agent families x two Harness tiers. The
two Harness tiers used in the paper:

  - Large Harness:  anthropic/claude-opus-4-7  (frontier reference)
  - Small Harness:  gemma-4-E4B-it-MLX-8bit    (4B local, via LM Studio)

Example — small local Harness LLM (Gemma 4B in LM Studio on port 1234)
evaluating the GPT-5.5 medical triage agent:

    python benchmarks/asymmetric_cohort.py \\
        --agent          medical_triage_assistant \\
        --agent-llm      gpt-5.5 \\
        --harness-llm    gemma-4-E4B-it-MLX-8bit \\
        --proxy-url      http://localhost:1234/v1 \\
        --turns          25 \\
        --seed           42 \\
        --consensus      debate \\
        --context-budget 6000 \\
        --sequential

Example — large cloud Harness LLM (Opus 4.7) evaluating the same agent (no
proxy, no sequential flag, cloud handles parallel calls):

    python benchmarks/asymmetric_cohort.py \\
        --agent          medical_triage_assistant \\
        --agent-llm      gpt-5.5 \\
        --harness-llm    anthropic/claude-opus-4-7 \\
        --turns          25 \\
        --seed           42 \\
        --consensus      debate

Cost / runtime guidance
-----------------------
At 25 turns x debate consensus x 3 personas, expect roughly 50-70 Harness LLM
calls per cell.

  - Cloud Harness LLM (Haiku 4.5 / Opus 4.7): $1-5 per cell, 5-15 min wall clock.
  - Local Harness LLM via LM Studio: $0 per cell, 25-45 min wall clock with
    --sequential. The local proxy is single-threaded; --sequential serializes
    juror calls so they don't timeout in the queue.

Required env
------------
  OPENAI_API_KEY     (for gpt-* agent LLM)
  ANTHROPIC_API_KEY  (for anthropic/claude-* agent LLM and/or Harness LLM)
  GEMINI_API_KEY     (for gemini/* agent LLM)
  Local proxy at --proxy-url (no auth required for most local servers)

Output
------
  ./results/asymmetric_<timestamp>/<agent-name>_harness-<harness>_agent-<agent>_<turns>turn_seed<N>.json
  ./results/asymmetric_<timestamp>/<agent-name>_harness-<harness>_agent-<agent>_<turns>turn_seed<N>.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from proofagent_harness import Harness

# ─────────────────────────────────────────────────────────────────────────────
# This script lives in benchmarks/ but reuses the shared assets that live in
# examples/: the agent factory + JSON specs (examples/agents/), the proxy-wiring
# helper from 01_quickstart.py, and the dashboard-push helper (_dashboard.py).
# Point at examples/ so all three resolve no matter where the script runs from.
# Putting examples/ on sys.path also makes `from agents...` import the
# examples/agents package directly.
# ─────────────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent                       # benchmarks/
_EXAMPLES_DIR = _HERE.parent / "examples"                     # examples/
sys.path.insert(0, str(_EXAMPLES_DIR))

# Optional governance-dashboard push (no-op offline). Sibling helper in examples/.
from _dashboard import push_to_dashboard  # noqa: E402

_QS_PATH = _EXAMPLES_DIR / "01_quickstart.py"
_qs_spec = importlib.util.spec_from_file_location("quickstart_helpers", _QS_PATH)
assert _qs_spec and _qs_spec.loader, f"01_quickstart.py not found at {_QS_PATH}"
_qs = importlib.util.module_from_spec(_qs_spec)
sys.modules["quickstart_helpers"] = _qs
_qs_spec.loader.exec_module(_qs)

_wire_proxy_for_harness_llm = _qs._wire_proxy_for_harness_llm

_FACTORY_PATH = _EXAMPLES_DIR / "agents" / "factory.py"
_fac_spec = importlib.util.spec_from_file_location("agents_factory", _FACTORY_PATH)
assert _fac_spec and _fac_spec.loader, f"agents/factory.py not found at {_FACTORY_PATH}"
_factory = importlib.util.module_from_spec(_fac_spec)
sys.modules["agents_factory"] = _factory
_fac_spec.loader.exec_module(_factory)

load_agent_spec = _factory.load_agent_spec
make_agent_from_spec = _factory.make_agent_from_spec
make_context_from_spec = _factory.make_context_from_spec

AGENTS_DIR = _EXAMPLES_DIR / "agents"
RESULTS_DIR = _HERE.parent / "results"

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Agent resolution — accept short name, filename, or absolute path
# ─────────────────────────────────────────────────────────────────────────────


def resolve_agent_spec_path(agent: str) -> Path:
    """Resolve --agent to a JSON spec path.

    Accepts:
        - short name:    customer_support_agent
        - filename:      customer_support_agent.json
        - absolute path: /path/to/some_agent.json
    """
    p = Path(agent).expanduser()
    if p.is_absolute() and p.exists():
        return p
    candidates = [
        AGENTS_DIR / agent,
        AGENTS_DIR / f"{agent}.json",
        AGENTS_DIR / Path(agent).name,
    ]
    for c in candidates:
        if c.exists():
            return c
    available = sorted(p.stem for p in AGENTS_DIR.glob("*.json"))
    raise SystemExit(
        f"agent spec not found for '{agent}'. Tried: {[str(c) for c in candidates]}\n"
        f"  Available bundled agents:\n    " + "\n    ".join(available)
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Asymmetric single-cell evaluation — small Harness LLM (local "
            "proxy or cheap cloud) evaluating a frontier agent. Reproduces "
            "the cohort cells reported in the paper."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Cloud Harness (no proxy needed)\n"
            "  python benchmarks/asymmetric_cohort.py \\\n"
            "      --agent       medical_triage_assistant \\\n"
            "      --agent-llm   gpt-5.5 \\\n"
            "      --harness-llm anthropic/claude-haiku-4-5 \\\n"
            "      --turns 25 --seed 42 --consensus debate\n"
            "\n"
            "  # Local Harness via LM Studio at localhost:1234\n"
            "  python benchmarks/asymmetric_cohort.py \\\n"
            "      --agent       customer_support_agent \\\n"
            "      --agent-llm   gpt-5.5 \\\n"
            "      --harness-llm gemma-4-E4B-it-MLX-8bit \\\n"
            "      --proxy-url   http://localhost:1234/v1 \\\n"
            "      --turns 25 --seed 42 --consensus debate \\\n"
            "      --context-budget 6000 --sequential\n"
        ),
    )

    # ── Required: agent + harness wiring
    p.add_argument(
        "--agent", required=True,
        help=(
            "Agent name to evaluate. Accepts a short name "
            "(customer_support_agent), a filename (...json), or an absolute "
            "path. Bundled agents live in examples/agents/."
        ),
    )
    p.add_argument(
        "--harness-llm", required=True, metavar="MODEL",
        help=(
            "Model powering the Harness LLM (planner, conductor, juror, "
            "reporter). Cloud examples: anthropic/claude-opus-4-7, "
            "anthropic/claude-haiku-4-5, gpt-5.5. Local example (via "
            "--proxy-url): gemma-4-E4B-it-MLX-8bit. "
            "When --proxy-url is set, the model name is auto-prefixed with "
            "'openai/' so LiteLLM routes through the OpenAI-compatible client."
        ),
    )

    # ── Optional: local proxy for the Harness LLM
    p.add_argument(
        "--proxy-url", required=False, default=None, metavar="URL",
        help=(
            "OpenAI-compatible URL for a LOCAL Harness LLM proxy. Examples: "
            "http://localhost:1234/v1 (LM Studio), "
            "http://localhost:11434/v1 (Ollama), "
            "http://localhost:8000/v1 (vLLM). "
            "OMIT this flag when --harness-llm is a real cloud model — "
            "LiteLLM will route it directly and no proxy is needed."
        ),
    )

    # ── Agent under test
    p.add_argument(
        "--agent-llm", default="gpt-4.1", metavar="MODEL",
        help=(
            "Model powering the agent under test. Auto-detects provider by "
            "name: gpt-* → OpenAI, anthropic/claude-* → Anthropic, gemini/* "
            "→ LiteLLM. Default: gpt-4.1."
        ),
    )

    # ── Run knobs
    p.add_argument(
        "--turns", "-t", type=int, default=25,
        help=(
            "Number of adversarial conductor turns. Default 25 (paper "
            "cohort). Use 50-100 for bracketed replication."
        ),
    )
    p.add_argument(
        "--seed", "-s", type=int, default=42,
        help="Random seed (default: 42).",
    )
    p.add_argument(
        "--consensus", "-c",
        choices=["independent", "delphi", "debate"], default="debate",
        help=(
            "Jury consensus strategy. independent (cheapest, 1x), delphi "
            "(balanced, ~1.5x), debate (strictest, 3-5x). Paper uses debate."
        ),
    )
    p.add_argument(
        "--context-budget", "--ctx", type=int, default=None, metavar="TOKENS",
        help=(
            "Juror context budget in tokens. REQUIRED for small-context "
            "proxy models. Use 6000 for 8K-context Gemma 4B; 16000 for "
            "32K-context models; 24000 for 128K-context models. Auto-detected "
            "from the model name if omitted for known cloud models."
        ),
    )

    # ── Output
    p.add_argument(
        "--output-dir", "-o", type=str, default=None, metavar="DIR",
        help=(
            "Where to write JSON + Markdown reports. Default: "
            "./results/asymmetric_<timestamp>/."
        ),
    )

    # ── Sequential mode for local single-threaded proxies
    p.add_argument(
        "--sequential", action="store_true",
        help=(
            "Serialize all juror LLM calls through a single asyncio "
            "Semaphore(1). REQUIRED when the local proxy (LM Studio, Ollama "
            "default) can only process one request at a time. Has no effect "
            "on cloud Harness LLMs (cloud handles parallel calls natively)."
        ),
    )
    p.add_argument(
        "--per-call-timeout", type=int, default=3600, metavar="SECONDS",
        help=(
            "Per-juror-call timeout when --sequential is set. Default 3600s "
            "(1 hr). Set comfortably above your slowest individual juror "
            "call; LiteLLM's default 600s is too aggressive for queued local "
            "proxy traffic."
        ),
    )

    # ── Wiring sanity check
    p.add_argument(
        "--list-only", action="store_true",
        help=(
            "Print resolved config + bundled agent list and exit (no API "
            "calls). Use this to verify wiring before spending tokens."
        ),
    )

    # ── Verbosity
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-turn progress output (still prints final scorecard).",
    )

    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Sequential jury patch — for local proxies that can't handle parallel calls
# ─────────────────────────────────────────────────────────────────────────────


def install_sequential_jury_calls(per_call_timeout: int, verbose: bool) -> None:
    """Force all LiteLLM calls (which the Harness LLM uses) to execute one
    at a time through a per-event-loop asyncio Semaphore(1).

    Why this exists: LM Studio's default server is single-threaded — it
    processes one chat completion at a time. The harness's jury stage fires
    up to 18 calls in parallel (3 personas x 6 metrics). With the default
    LiteLLM timeout of 600s, calls at the back of the queue time out while
    still waiting to start. This causes round-1 cascade failures even on
    simple prompts.

    Uses lazy per-event-loop semaphores because the harness creates a fresh
    event loop per evaluation; a single global semaphore bound to the first
    loop fails on the second eval with "Semaphore bound to a different event
    loop".

    Cloud Harness LLMs do not need this — they handle real parallelism.
    """
    import asyncio
    import litellm

    _orig_acompletion = litellm.acompletion
    _orig_completion = litellm.completion
    _sem_by_loop: dict[int, asyncio.Semaphore] = {}
    _call_n = [0]

    def _get_sem() -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        key = id(loop)
        if key not in _sem_by_loop:
            _sem_by_loop[key] = asyncio.Semaphore(1)
        return _sem_by_loop[key]

    async def _serial_acompletion(*args, **kwargs):
        sem = _get_sem()
        async with sem:
            _call_n[0] += 1
            n = _call_n[0]
            kwargs.setdefault("timeout", per_call_timeout)
            kwargs.setdefault("request_timeout", per_call_timeout)
            if verbose:
                console.print(f"  [dim][jury-serial #{n}] start[/dim]")
            t0 = time.time()
            try:
                r = await _orig_acompletion(*args, **kwargs)
                if verbose:
                    console.print(
                        f"  [dim][jury-serial #{n}] done in {time.time()-t0:.1f}s[/dim]"
                    )
                return r
            except Exception as e:
                if verbose:
                    console.print(
                        f"  [yellow][jury-serial #{n}] failed in {time.time()-t0:.1f}s: "
                        f"{type(e).__name__}: {str(e)[:120]}[/yellow]"
                    )
                raise

    def _serial_completion(*args, **kwargs):
        kwargs.setdefault("timeout", per_call_timeout)
        kwargs.setdefault("request_timeout", per_call_timeout)
        return _orig_completion(*args, **kwargs)

    litellm.acompletion = _serial_acompletion
    litellm.completion = _serial_completion
    console.print(
        f"  [yellow][config][/yellow] sequential jury mode ON  "
        f"(per-call timeout {per_call_timeout}s, semaphore=1)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def _safe(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s)


def _snapshot_openai_env() -> tuple[str | None, str | None]:
    return os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_API_KEY")


def _restore_openai_env(saved: tuple[str | None, str | None]) -> None:
    saved_base, saved_key = saved
    if saved_base is None:
        os.environ.pop("OPENAI_BASE_URL", None)
    else:
        os.environ["OPENAI_BASE_URL"] = saved_base
    if saved_key is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = saved_key


def main() -> int:
    args = parse_args()
    if args.turns < 1:
        raise SystemExit("--turns must be >= 1")

    spec_path = resolve_agent_spec_path(args.agent)
    spec = load_agent_spec(spec_path)

    # ── Output layout
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        out_dir = Path(args.output_dir).expanduser().resolve()
    else:
        out_dir = RESULTS_DIR / f"asymmetric_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{spec.name}"
        f"_harness-{_safe(args.harness_llm)}"
        f"_agent-{_safe(args.agent_llm)}"
        f"_{args.turns}turn_seed{args.seed}"
    )
    out_json = out_dir / f"{stem}.json"
    out_md = out_dir / f"{stem}.md"

    # ── Pre-flight banner
    console.print(Rule("[bold cyan]Asymmetric single-cell evaluation"))
    cfg = Table.grid(padding=(0, 2))
    cfg.add_column(style="dim", justify="right")
    cfg.add_column()
    cfg.add_row("agent spec", f"{spec_path}")
    cfg.add_row("agent name", f"{spec.name}  (domain: {spec.domain})")
    cfg.add_row("agent LLM", f"{args.agent_llm}")
    wire = f"via {args.proxy_url}" if args.proxy_url else "direct (cloud)"
    cfg.add_row("harness LLM", f"{args.harness_llm}   ({wire})")
    cfg.add_row("turns", f"{args.turns}")
    cfg.add_row("consensus", f"{args.consensus}")
    cfg.add_row("seed", f"{args.seed}")
    cfg.add_row("ctx budget", f"{args.context_budget if args.context_budget else 'auto'} tokens")
    cfg.add_row("output dir", f"{out_dir}")
    console.print(Panel(cfg, title="config", border_style="cyan"))

    if args.list_only:
        available = sorted(p.stem for p in AGENTS_DIR.glob("*.json"))
        console.print("\n[bold]Bundled agents available:[/bold]")
        for a in available:
            console.print(f"  • {a}")
        console.print("\n[--list-only] No eval run. Drop --list-only to evaluate.")
        return 0

    # ── Wire the proxy + sequential patch (only when using a local proxy).
    saved_env = _snapshot_openai_env()
    if args.proxy_url:
        harness_llm = _wire_proxy_for_harness_llm(args.proxy_url, args.harness_llm)
        if args.sequential:
            install_sequential_jury_calls(
                per_call_timeout=args.per_call_timeout,
                verbose=not args.quiet,
            )
    else:
        harness_llm = args.harness_llm
        if args.sequential:
            console.print(
                "  [yellow][config][/yellow] --sequential ignored "
                "(no --proxy-url; cloud LLMs handle parallel calls natively)"
            )

    # ── Build the agent + context from the spec
    agent_callable = make_agent_from_spec(spec, model=args.agent_llm)
    agent_context = make_context_from_spec(spec)

    if args.turns > 64:
        console.print(
            f"  [yellow][note][/yellow] {args.turns} turns > 64 bundled trap "
            f"families; the planner will reuse traps with fresh seeds for the "
            f"later turns."
        )

    # ── Run
    t0 = time.time()
    try:
        harness_kwargs: dict[str, Any] = dict(
            llm=harness_llm,
            turns=args.turns,
            consensus=args.consensus,
            seed=args.seed,
            verbose=not args.quiet,
        )
        if args.context_budget:
            harness_kwargs["context_budget_tokens"] = args.context_budget
        report = Harness(**harness_kwargs).evaluate(
            agent_callable,
            role=spec.role,
            business_case=spec.business_case,
            goal=spec.goal,
            context=agent_context,
        )
    except Exception as exc:
        elapsed = time.time() - t0
        console.print(
            f"  [red]✗ {spec.name} FAILED[/red] after {elapsed:.0f}s — "
            f"{type(exc).__name__}: {exc}"
        )
        _restore_openai_env(saved_env)
        return 1
    finally:
        _restore_openai_env(saved_env)

    elapsed = time.time() - t0
    report.to_json(str(out_json))
    report.to_markdown(str(out_md))

    # ── OPTIONAL: push this run to the ProofAgent Governance dashboard ──
    #    No-op offline. The agent_name embeds the swept --agent so each agent
    #    in a sweep lands as its own dashboard run.
    push_to_dashboard(
        report,
        agent_name=f"asymmetric-{spec.name.replace('/', '-')}",
        source="local",
    )

    # ── Per-agent scorecard
    score = getattr(report, "final_score", 0.0) or 0.0
    cert = str(getattr(report, "certification", ""))
    per_metric = getattr(report, "per_metric", {}) or {}
    confidence = getattr(report, "confidence", {}) or {}

    score_tbl = Table(
        title=f"{spec.name} — scorecard",
        show_header=True, header_style="bold",
        border_style=("green" if score >= 8.5 else "yellow" if score >= 7.0 else "red"),
    )
    score_tbl.add_column("metric", style="dim")
    score_tbl.add_column("score", justify="right")
    score_tbl.add_column("confidence", justify="right")
    for m, s in per_metric.items():
        c = confidence.get(m, "-")
        score_tbl.add_row(m, f"{float(s):.2f}", f"{c}")
    console.print(score_tbl)
    console.print(
        Panel(
            f"[bold]final score:[/bold] {score}    "
            f"[bold]certification:[/bold] {cert}    "
            f"[dim]duration:[/dim] {elapsed:.0f}s",
            border_style=("green" if any(t in cert for t in ("GOLD", "SILVER")) else "yellow"),
        )
    )
    console.print(f"  report json: {out_json}")
    console.print(f"  report md:   {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
