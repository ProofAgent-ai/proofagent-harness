"""Advanced observability example — live cumulative trace of an evaluation.

This is an **observability / debugging** example. It runs the harness against
one of the bundled agents and prints a rich panel per turn so you can SEE
what the conductor is picking, what it's asking, what the agent answers, and
how coverage is accumulating turn by turn. Use this when you want to
understand *why* an agent failed, not when you want a batch eval score.

For canonical batch evaluation, prefer `09_asymmetric_single_cell.py`. This
example shares the same agent specs (`examples/agents/*.json`) and the same
factory (`examples/agents/factory.py`) — no duplication.

What this example uniquely shows
--------------------------------
* `Harness.evaluate(on_event=...)` — the official streaming hook. Every
  `turn_start` event tells you which trap the planner picked.
* Wrapping the agent callable to capture per-turn Q + A live. Useful for
  building production telemetry, SIEM forwarders, or dataset collection.
* Rendering the full trap card (id, family, severity, metrics, forbidden
  / expected tools, pattern excerpt including the composite attack chain
  injected into Pattern) BEFORE each turn so you can see what's about to
  hit the agent.
* A cumulative coverage line that evolves turn by turn: unique traps used,
  families covered, severity mix, composite chains seen, agent crashes.

Agent registry
--------------
Five production-style agent specs live in `examples/agents/`:

    --agent customer_support_agent        AcmeAir refund/itinerary agent
    --agent medical_triage_assistant      Telehealth triage assistant
    --agent privacy_security_agent        GDPR / CCPA privacy ops agent
    --agent code_generation_agent         Internal engineering code assistant
    --agent financial_advisor_agent       FINRA-registered investment advisor

Pass a custom JSON spec via `--agent /path/to/your_agent.json` to evaluate
your own agent. The spec schema is documented in `examples/agents/README.md`.

Usage
-----
    # List available agents (no API calls)
    python examples/11_live_trace_evaluation.py --list-agents

    # Wire check (loads trap library + selected agent, no API calls)
    python examples/11_live_trace_evaluation.py --agent privacy_security_agent --list-only

    # Real run — local Gemma harness LLM + gpt-4.1-mini agent
    python examples/11_live_trace_evaluation.py \\
        --agent          medical_triage_assistant \\
        --agent-model    gpt-4.1-mini \\
        --harness-llm    gemma-4-E4B-it-MLX-8bit \\
        --proxy-url      http://localhost:1234/v1 \\
        --turns          8 \\
        --consensus      delphi \\
        --seed           42 \\
        --context-budget 6000 \\
        --sequential

    # Cross-family — Claude Opus agent + cloud Sonnet harness LLM
    python examples/11_live_trace_evaluation.py \\
        --agent privacy_security_agent \\
        --agent-model claude-opus-4-7 \\
        --harness-llm anthropic/claude-sonnet-4-6 \\
        --no-proxy --turns 10 --consensus debate

    # Verbose — no truncation of pattern / Q / A
    python examples/11_live_trace_evaluation.py \\
        --agent privacy_security_agent --verbose --turns 5 --sequential

    # Merge an external trap directory on top of the bundled 183-trap library.
    # Useful when you keep an experimental / private trap pack outside the
    # main harness repo. Custom traps override bundled traps with the same name
    # ("last wins").
    python examples/11_live_trace_evaluation.py \\
        --agent privacy_security_agent \\
        --extra-traps ~/projects/my-benchmark/hacker_traps/ \\
        --turns 8

    # Multiple external dirs + a pip-installed trap pack at the same time.
    python examples/11_live_trace_evaluation.py \\
        --agent customer_support_agent \\
        --extra-traps ./internal_traps/ ./pilot_traps/ \\
        --trap-packs finance healthcare \\
        --turns 8

    # Juror fallback — recommended whenever the primary is a small local model.
    # If Gemma returns empty/garbled juror output, retry with gpt-4.1-mini
    # (forced through api.openai.com). Without this, silent juror failures
    # collapse the final score to 0/10 with no findings. The fallback only
    # fires on actual failures, so cost is bounded by your primary's failure rate.
    python examples/11_live_trace_evaluation.py \\
        --agent           customer_support_agent \\
        --extra-traps     /path/to/hacker_traps/ \\
        --agent-model     gpt-4.1 \\
        --harness-llm     gemma-4-E4B-it-MLX-8bit \\
        --proxy-url       http://localhost:1234/v1 \\
        --fallback-juror  gpt-4.1-mini \\
        --consensus       debate --turns 100 --seed 42 --sequential \\
        --context-budget  100000 --per-call-timeout 7200

Required env
------------
    OPENAI_API_KEY     (gpt-* agent models)
    ANTHROPIC_API_KEY  (claude-* agent models)
    Local proxy at --proxy-url serving the harness LLM (or --no-proxy for cloud)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from proofagent_harness import (
    AgentContext,
    AgentResponse,
    Event,
    Harness,
    Trap,
    TrapIndex,
    load_traps,
)

console = Console()


# ═════════════════════════════════════════════════════════════════════════════
# Reuse the agent factory + spec loader from examples/agents/factory.py.
# Same pattern as 09_asymmetric_single_cell.py — single source of truth for
# agent construction across the examples folder.
# ═════════════════════════════════════════════════════════════════════════════


_HERE = Path(__file__).resolve().parent
AGENTS_DIR = _HERE / "agents"
DEFAULT_RESULTS_DIR = _HERE.parent / "results"

_FACTORY_PATH = AGENTS_DIR / "factory.py"
_fac_spec = importlib.util.spec_from_file_location("agents_factory", _FACTORY_PATH)
assert _fac_spec and _fac_spec.loader, f"agents/factory.py not found at {_FACTORY_PATH}"
_factory = importlib.util.module_from_spec(_fac_spec)
sys.modules["agents_factory"] = _factory
_fac_spec.loader.exec_module(_factory)

AgentSpec = _factory.AgentSpec
load_agent_spec = _factory.load_agent_spec
make_agent_from_spec = _factory.make_agent_from_spec
make_context_from_spec = _factory.make_context_from_spec


def discover_bundled_agents() -> list[str]:
    """Return short names of bundled agent specs under examples/agents/."""
    return sorted(p.stem for p in AGENTS_DIR.glob("*.json"))


def resolve_agent_spec_path(agent_arg: str) -> Path:
    """Accept short name (`customer_support_agent`), filename (`...json`), or
    absolute path. Mirrors 09_asymmetric_single_cell.py resolution."""
    p = Path(agent_arg).expanduser()
    if p.is_absolute() and p.exists():
        return p
    for c in (AGENTS_DIR / agent_arg, AGENTS_DIR / f"{agent_arg}.json", AGENTS_DIR / Path(agent_arg).name):
        if c.exists():
            return c
    raise SystemExit(
        f"--agent {agent_arg!r} not found. Available: {discover_bundled_agents()}. "
        f"Or pass an absolute path to a JSON spec."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Proxy wiring + sequential jury + think-tag stripper
#
# Inlined — small, self-contained, no cross-script imports. Same logic as
# 09_asymmetric_single_cell.py (kept consistent for parity across examples).
# ═════════════════════════════════════════════════════════════════════════════


def wire_proxy(proxy_url: str, llm: str) -> str:
    """Set OPENAI_BASE_URL so LiteLLM routes the harness LLM to a local
    proxy. Returns the (possibly prefixed) model name to pass into
    `Harness(llm=...)`."""
    os.environ["OPENAI_BASE_URL"] = proxy_url
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "not-required-for-local-proxy"
    if not llm.startswith(("openai/", "anthropic/", "gemini/", "bedrock/", "ollama/", "groq/")):
        return f"openai/{llm}"
    return llm


def install_sequential_jury(per_call_timeout: int, verbose: bool) -> None:
    """Serialize litellm calls. REQUIRED for single-threaded local proxies."""
    import litellm
    _orig = litellm.acompletion
    _sem_by_loop: dict[int, asyncio.Semaphore] = {}

    def _get_sem() -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        key = id(loop)
        if key not in _sem_by_loop:
            _sem_by_loop[key] = asyncio.Semaphore(1)
        return _sem_by_loop[key]

    async def _serial(*args, **kwargs):
        sem = _get_sem()
        async with sem:
            kwargs.setdefault("timeout", per_call_timeout)
            kwargs.setdefault("request_timeout", per_call_timeout)
            return await _orig(*args, **kwargs)
    litellm.acompletion = _serial
    if verbose:
        console.print(f"  [yellow][config][/yellow] sequential jury mode ON (timeout {per_call_timeout}s)")


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_TRAIL = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_REASONING_HINTS = ("gemma", "deepseek-r1", "deepseek_r1", "qwq", "openai/o1", "openai/o3", "o1-", "o3-")


def install_fallback_juror(fallback_model: str, verbose: bool) -> dict:
    """If a juror call to the primary harness LLM returns empty/garbled content
    or raises an exception, automatically retry the same prompt with
    `fallback_model` (forced through api.openai.com, bypassing --proxy-url).

    Critical when running a small local harness LLM (e.g. Gemma 4B) at long
    context windows — Gemma occasionally returns empty content or content
    consisting only of `<think>` blocks the parser cannot use as a juror score.
    Without a fallback, those turns get silently skipped, and a run with many
    failures finishes with final_score=0/10 and no findings.

    The fallback costs only what's needed (e.g. gpt-4.1-mini at ~$0.05-0.20
    per failed turn) but rescues the run from a silent failure. Requires
    `OPENAI_API_KEY` to be set with a real OpenAI key.

    Returns a tracker dict updated in-place as calls happen; pass it to
    `print_fallback_report()` at end of run for the usage breakdown.
    """
    import litellm
    _orig = litellm.acompletion

    tracker: dict = {
        "fallback_model": fallback_model,
        "primary_calls": 0,
        "primary_empty": 0,
        "primary_errors": 0,
        "fallback_calls": 0,
        "fallback_errors": 0,
    }

    # Normalize: if user passed bare "gpt-4.1-mini", prefix to "openai/gpt-4.1-mini"
    # so LiteLLM routes it via the OpenAI provider.
    fb_model = fallback_model if "/" in fallback_model else f"openai/{fallback_model}"

    def _is_useful(response) -> bool:
        try:
            choices = getattr(response, "choices", None) or []
            if not choices:
                return False
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", "") if msg else ""
            if not isinstance(content, str):
                return False
            # Re-strip any leftover think tags the upstream wrapper missed.
            cleaned = _THINK_RE.sub("", content)
            cleaned = _THINK_TRAIL.sub("", cleaned).strip()
            return len(cleaned) >= 30
        except Exception:
            return False

    async def _with_fallback(*args, **kwargs):
        tracker["primary_calls"] += 1
        # ── Primary attempt ──────────────────────────────────────────
        try:
            r = await _orig(*args, **kwargs)
            if _is_useful(r):
                return r
            tracker["primary_empty"] += 1
            if verbose:
                console.print(
                    f"  [yellow][fallback][/yellow] primary returned empty/garbled "
                    f"(call #{tracker['primary_calls']}); retrying with {fallback_model}"
                )
        except Exception as exc:
            tracker["primary_errors"] += 1
            if verbose:
                console.print(
                    f"  [yellow][fallback][/yellow] primary raised "
                    f"{type(exc).__name__} (call #{tracker['primary_calls']}); "
                    f"retrying with {fallback_model}"
                )
        # ── Fallback attempt ─────────────────────────────────────────
        # Same args, different model + force api.openai.com (bypass --proxy-url
        # which is set via OPENAI_BASE_URL). Per-call api_base overrides env.
        fb_kwargs = {**kwargs, "model": fb_model, "api_base": "https://api.openai.com/v1"}
        try:
            r = await _orig(*args, **fb_kwargs)
            tracker["fallback_calls"] += 1
            return r
        except Exception as exc:
            tracker["fallback_errors"] += 1
            console.print(
                f"  [red][fallback][/red] fallback {fallback_model} ALSO failed: "
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )
            raise

    litellm.acompletion = _with_fallback
    if verbose:
        console.print(
            f"  [yellow][config][/yellow] juror fallback ON "
            f"(primary fail → retry with {fallback_model} via api.openai.com)"
        )
    return tracker


def print_fallback_report(tracker: dict | None) -> None:
    """Print the juror-fallback usage breakdown at end of run."""
    if not tracker or tracker.get("primary_calls", 0) == 0:
        return
    pc = tracker["primary_calls"]
    pe = tracker["primary_empty"]
    perr = tracker["primary_errors"]
    fc = tracker["fallback_calls"]
    ferr = tracker["fallback_errors"]
    ok_pct = ((pc - pe - perr) / pc) * 100 if pc else 0.0
    fb_pct = (fc / pc) * 100 if pc else 0.0
    body = (
        f"[bold]Primary juror calls:[/bold]   {pc}\n"
        f"[bold]   returned OK:[/bold]        {pc - pe - perr}  ({ok_pct:.1f}%)\n"
        f"[bold]   empty / garbled:[/bold]    {pe}\n"
        f"[bold]   raised exception:[/bold]   {perr}\n"
        f"[bold]Fallback used:[/bold]         {fc}  ({fb_pct:.1f}% of all calls)\n"
        f"[bold]   fallback errors:[/bold]    {ferr}"
    )
    border = "green" if fc == 0 else ("yellow" if ferr == 0 else "red")
    console.print(
        Panel(body, title=f"Juror fallback report ({tracker['fallback_model']})",
              border_style=border)
    )


def install_think_stripper(harness_llm: str, verbose: bool) -> bool:
    """Strip <think>...</think> reasoning tags from litellm responses so the
    juror JSON parser doesn't choke on them."""
    if not any(p in harness_llm.lower() for p in _REASONING_HINTS):
        return False
    import litellm
    _orig = litellm.acompletion

    async def _patched(*args, **kwargs):
        r = await _orig(*args, **kwargs)
        try:
            for ch in (getattr(r, "choices", None) or []):
                msg = getattr(ch, "message", None)
                txt = getattr(msg, "content", None) if hasattr(msg, "content") else None
                if isinstance(txt, str) and "<think" in txt.lower():
                    cleaned = _THINK_RE.sub("", txt)
                    cleaned = _THINK_TRAIL.sub("", cleaned).strip()
                    if cleaned:
                        msg.content = cleaned
        except Exception:
            pass  # never crash the response path
        return r
    litellm.acompletion = _patched
    if verbose:
        console.print(f"  [yellow][config][/yellow] think-tag stripper ON ({harness_llm!r})")
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Live trace collector
#
# Subscribes to the harness's on_event callback for trap-selection events.
# Wraps the agent callable to capture each turn's Q + A in real time.
# Renders rich panels for the trap card, conductor question, agent answer,
# tool calls, and a cumulative coverage line.
# ═════════════════════════════════════════════════════════════════════════════


class LiveTraceCollector:
    TRAP_RE = re.compile(r"trap:\s*(\S+)")

    def __init__(self, trap_index: TrapIndex, verbose: bool):
        self.idx = trap_index
        self.verbose = verbose
        self.turn_n = 0
        self.current_trap: Trap | None = None
        self.traps_used: list[str] = []
        self.families_seen: Counter[str] = Counter()
        self.severities_seen: Counter[str] = Counter()
        self.composite_traps: set[str] = set()
        self.crashes = 0

    def on_event(self, ev: Event) -> None:
        if ev.type == "turn_start" and ev.detail:
            m = self.TRAP_RE.search(ev.detail)
            if not m:
                return
            self.current_trap = self.idx.by_name.get(m.group(1))
            self.turn_n = ev.turn or (self.turn_n + 1)
            if self.current_trap:
                self.traps_used.append(self.current_trap.name)
                self.families_seen[self.current_trap.family] += 1
                self.severities_seen[self.current_trap.severity] += 1
                if "Composite attack chain" in (self.current_trap.pattern or ""):
                    self.composite_traps.add(self.current_trap.name)
                self._render_trap_card(self.current_trap, ev.detail)

    def wrap_agent(self, agent: Callable[[str], AgentResponse]) -> Callable[[str], AgentResponse]:
        def wrapped(message: str) -> AgentResponse:
            self._render_question(message)
            t0 = time.time()
            try:
                resp = agent(message)
                self._render_answer(resp, time.time() - t0)
                return resp
            except Exception as exc:
                self.crashes += 1
                self._render_crash(exc, time.time() - t0)
                raise
        return wrapped

    def _render_trap_card(self, trap: Trap, detail: str) -> None:
        family_color = {
            "prompt_injection": "magenta", "social_engineering": "red",
            "data_exfiltration": "yellow", "compliance": "cyan",
            "factuality": "blue", "tool_misuse": "bright_red",
            "code_safety": "bright_yellow", "verbal_abuse": "red",
            "business_logic": "green", "policy_drift": "bright_blue",
            "bias": "white",
        }.get(trap.family, "white")
        sev = {"critical": "[red]CRITICAL[/red]", "high": "[bright_red]HIGH[/bright_red]",
               "medium": "[yellow]MEDIUM[/yellow]", "low": "[green]LOW[/green]"}.get(trap.severity, trap.severity)
        composite = " [bright_magenta][composite chain present][/bright_magenta]" if "Composite attack chain" in (trap.pattern or "") else ""

        pattern_show = trap.pattern if self.verbose else (
            (trap.pattern or "")[:400] + ("  …[dim](truncated, --verbose for full)[/dim]" if len(trap.pattern or "") > 400 else "")
        )
        seed = trap.seeds[0] if trap.seeds else ""
        if seed and not self.verbose and len(seed) > 200:
            seed = seed[:200] + "…"
        seed_show = f"\n[bold]Seed[/bold] (turn opener):\n  \"{seed}\"" if seed else ""

        body = (
            f"[bold]Trap ID:[/bold]   [{family_color}]{trap.family}[/{family_color}] / {trap.name}{composite}\n"
            f"[bold]Severity:[/bold]  {sev}\n"
            f"[bold]Metrics:[/bold]   {' · '.join(trap.metrics) or '(none)'}\n"
            f"[bold]Reach:[/bold]     {'universal' if trap.universal else 'domains=' + ','.join(trap.domains or ['none'])}\n"
            f"[bold]Forbidden:[/bold] {' · '.join(trap.forbidden_tools) or '(none)'}\n"
            f"[bold]Expected:[/bold]  {' · '.join(trap.expected_tools) or '(none)'}\n"
            f"[bold]Tags:[/bold]      {' · '.join(trap.tags) or '(none)'}\n"
            f"\n[bold]Pattern[/bold] (incl. composite chain when injected):\n{pattern_show}"
            f"{seed_show}\n"
            f"\n[dim]Event detail: {detail}[/dim]"
        )
        console.print(Rule(f"[bold {family_color}]TURN {self.turn_n}[/bold {family_color}]"))
        console.print(Panel(body, title="selected trap", border_style=family_color))

    def _render_question(self, q: str) -> None:
        show = q if self.verbose else (q[:1200] + ("\n…[dim](truncated)[/dim]" if len(q) > 1200 else ""))
        console.print(Panel(show, title="◀ CONDUCTOR Q (adversarial probe)", border_style="bright_red"))

    def _render_answer(self, resp: AgentResponse, elapsed: float) -> None:
        ans = resp.text or "[dim](empty response)[/dim]"
        show = ans if self.verbose else (ans[:1200] + ("\n…[dim](truncated)[/dim]" if len(ans) > 1200 else ""))
        console.print(Panel(show, title=f"▶ AGENT A  ({elapsed:.1f}s)", border_style="green"))
        if resp.tools_called:
            tbl = Table(show_header=True, header_style="bold", title="tools called")
            tbl.add_column("tool", style="cyan")
            tbl.add_column("args", style="dim")
            tbl.add_column("result", style="dim")
            for tc in resp.tools_called:
                a = json.dumps(tc.get("args", {}), separators=(",", ":"))
                r = json.dumps(tc.get("result", {}), separators=(",", ":"))
                if not self.verbose:
                    a = a[:100] + "…" if len(a) > 100 else a
                    r = r[:100] + "…" if len(r) > 100 else r
                tbl.add_row(tc.get("name", "?"), a, r)
            console.print(tbl)
        self._render_cumulative()

    def _render_crash(self, exc: Exception, elapsed: float) -> None:
        console.print(Panel(f"{type(exc).__name__}: {exc}",
                            title=f"✗ AGENT CRASHED ({elapsed:.1f}s)", border_style="bright_red"))
        self._render_cumulative()

    def _render_cumulative(self) -> None:
        unique = len(set(self.traps_used))
        fam_str = ", ".join(f"{f}({n})" for f, n in self.families_seen.most_common())
        sev_str = " · ".join(f"{s}={n}" for s, n in self.severities_seen.most_common())
        # NOTE: avoid bare [...] — rich parses square brackets as markup tags.
        console.print(Panel(
            f"[bold]CUMULATIVE[/bold]: turn={self.turn_n} · unique_traps={unique} · "
            f"families={len(self.families_seen)} ({fam_str}) · "
            f"severity_mix=⟨{sev_str}⟩ · "
            f"composite_chains_used={len(self.composite_traps)} · "
            f"agent_crashes={self.crashes}",
            border_style="dim", padding=(0, 1),
        ))
        console.print()


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    bundled = discover_bundled_agents()
    p = argparse.ArgumentParser(
        prog="11_live_trace_evaluation.py",
        description=(
            "Advanced observability example — live cumulative trace of an "
            "evaluation. Watch the harness pick traps and probe the agent in "
            "real time. Use for debugging WHY an agent failed; for batch "
            "evaluation see 09_asymmetric_single_cell.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Agent selection ──────────────────────────────────────────────
    p.add_argument(
        "--agent", "-a", default="customer_support_agent",
        help=(
            f"Bundled agent name (one of: {', '.join(bundled)}) or absolute "
            f"path to a custom JSON spec following the schema in "
            f"examples/agents/README.md. Default: customer_support_agent."
        ),
    )

    # ── LLM wiring ───────────────────────────────────────────────────
    p.add_argument("--agent-model", default="gpt-4.1-mini",
                   help="LLM that powers the agent under test. Default: gpt-4.1-mini.")
    p.add_argument("--harness-llm", default="gemma-4-E4B-it-MLX-8bit",
                   help="LLM that powers the conductor + jury panel. Default: gemma-4-E4B-it-MLX-8bit.")
    p.add_argument("--proxy-url", default="http://localhost:1234/v1",
                   help="Local proxy URL for the harness LLM. Default: LM Studio at :1234.")
    p.add_argument("--no-proxy", action="store_true",
                   help="Skip proxy wiring (use when --harness-llm is a cloud model).")

    # ── Run knobs ────────────────────────────────────────────────────
    p.add_argument("--turns", "-t", type=int, default=8)
    p.add_argument("--consensus", "-c", choices=["independent", "delphi", "debate"], default="delphi")
    p.add_argument("--seed", "-s", type=int, default=42)
    p.add_argument("--context-budget", "--ctx", type=int, default=6000)
    p.add_argument("--sequential", action="store_true",
                   help="Serialize juror calls — REQUIRED for single-threaded local proxies.")
    p.add_argument("--per-call-timeout", type=int, default=3600)

    # ── Juror fallback (rescue from silent juror failures) ───────────
    p.add_argument(
        "--fallback-juror", default=None, metavar="MODEL",
        help=(
            "When the primary --harness-llm returns empty/garbled content or "
            "raises an exception on a juror call, automatically retry the same "
            "prompt with MODEL (forced through api.openai.com, bypassing "
            "--proxy-url). Critical when the primary is a small local model "
            "(e.g. Gemma 4B) that occasionally fails JSON-formatted juror "
            "scoring — without this, failed turns are silently dropped and "
            "the final score collapses to 0. Recommended: 'gpt-4.1-mini' "
            "(~$0.05-0.20 per fallback). Requires OPENAI_API_KEY set with a "
            "real OpenAI key."
        ),
    )

    # ── External trap sources (merge on top of bundled library) ──────
    p.add_argument(
        "--extra-traps", nargs="+", default=None, metavar="PATH",
        help=(
            "One or more directories of custom .md trap files to merge on top "
            "of the bundled library. Useful for experimental / private trap "
            "packs you don't want to ship in the main repo. Custom traps "
            "override bundled traps with the same name (last wins). Each path "
            "is recursed; both flat and family-subdir layouts are accepted."
        ),
    )
    p.add_argument(
        "--trap-packs", nargs="+", default=None, metavar="PACK",
        help=(
            "One or more pip-installed trap packs to load (e.g. 'finance' "
            "loads the proofagent_traps_finance package via importlib resources). "
            "Combines additively with --extra-traps and the bundled library."
        ),
    )

    # ── Verbosity / output ───────────────────────────────────────────
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--list-only", action="store_true",
                   help="Print config + selected agent summary + trap library, no API calls.")
    p.add_argument("--list-agents", action="store_true",
                   help="Print the bundled agent registry and exit.")
    p.add_argument("--output-dir", default=None)
    return p.parse_args()


def print_agent_registry() -> None:
    tbl = Table(title="Bundled agent registry (examples/agents/)", show_header=True, header_style="bold")
    tbl.add_column("--agent", style="cyan")
    tbl.add_column("role", style="white")
    tbl.add_column("tools", justify="right")
    tbl.add_column("knowledge", justify="right")
    for name in discover_bundled_agents():
        try:
            spec = load_agent_spec(AGENTS_DIR / f"{name}.json")
            tbl.add_row(name, spec.role[:70] + ("…" if len(spec.role) > 70 else ""),
                        str(len(spec.tools_openai)), f"{len(spec.knowledge)} chars")
        except Exception as e:
            tbl.add_row(name, f"[red](failed to load: {e})[/red]", "-", "-")
    console.print(tbl)
    console.print(
        "\n[dim]Use --agent <name> to select. Use --agent /path/to/your.json for a custom spec. "
        "Spec schema: examples/agents/README.md.[/dim]"
    )


def print_selected_spec(spec: AgentSpec) -> None:
    body = (
        f"[bold]Name:[/bold]          {spec.name}\n"
        f"[bold]Role:[/bold]          {spec.role}\n"
        f"[bold]Domain:[/bold]        {spec.domain}\n"
        f"[bold]Business case:[/bold] {spec.business_case}\n"
        f"[bold]Goal:[/bold]          {spec.goal}\n"
        f"[bold]Tools:[/bold]         {len(spec.tools_openai)} ({', '.join(t['function']['name'] for t in spec.tools_openai[:6])}{'…' if len(spec.tools_openai) > 6 else ''})\n"
        f"[bold]Knowledge:[/bold]     {len(spec.knowledge)} chars\n"
        f"[bold]System prompt:[/bold] {len(spec.system_prompt)} chars\n"
        f"[bold]Skills:[/bold]        {len(spec.skills)} · [bold]Guardrails:[/bold] {len(spec.guardrails)}"
    )
    console.print(Panel(body, title="selected agent spec", border_style="cyan"))


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════


def main() -> int:
    args = parse_args()

    if args.list_agents:
        print_agent_registry()
        return 0

    spec_path = resolve_agent_spec_path(args.agent)
    spec = load_agent_spec(spec_path)

    console.print(
        Panel.fit(
            "[bold cyan]Advanced observability example — live trace[/bold cyan]\n"
            "Watching the harness pick traps and probe the agent in real time.\n"
            "[dim]For batch evaluation use 09_asymmetric_single_cell.py.[/dim]",
            border_style="cyan",
        )
    )
    console.print(f"  Agent spec:     [bold]{spec.name}[/bold]  ([dim]{spec_path}[/dim])")
    console.print(f"  Agent model:    [bold]{args.agent_model}[/bold]")
    console.print(f"  Harness LLM:    [bold]{args.harness_llm}[/bold]")
    if not args.no_proxy:
        console.print(f"  Proxy URL:      [bold]{args.proxy_url}[/bold]")
    console.print(f"  Turns:          [bold]{args.turns}[/bold]")
    console.print(f"  Consensus:      [bold]{args.consensus}[/bold]")
    console.print(f"  Seed:           [bold]{args.seed}[/bold]")
    console.print(f"  Sequential:     [bold]{args.sequential}[/bold]")
    console.print()
    print_selected_spec(spec)
    console.print()

    # Validate external trap paths up-front (fail fast with a clear message
    # instead of a downstream FileNotFoundError after the harness has booted).
    extra_traps = args.extra_traps or []
    trap_packs = args.trap_packs or []
    for raw in extra_traps:
        if not Path(raw).expanduser().exists():
            raise SystemExit(f"--extra-traps path does not exist: {raw}")
    extra_traps_resolved = [str(Path(p).expanduser().resolve()) for p in extra_traps]

    bundled_only = load_traps()
    traps = load_traps(extra_dirs=extra_traps_resolved, trap_packs=trap_packs)
    idx = TrapIndex(traps)
    composite_n = sum(1 for t in traps if "Composite attack chain" in (t.pattern or ""))

    extra_added = len(traps) - len(bundled_only)
    extras_line = ""
    if extra_traps_resolved or trap_packs:
        srcs = []
        if extra_traps_resolved:
            srcs.append(f"{len(extra_traps_resolved)} dir(s): " + ", ".join(extra_traps_resolved))
        if trap_packs:
            srcs.append(f"{len(trap_packs)} pack(s): " + ", ".join(trap_packs))
        extras_line = (
            f"\n[dim]External traps merged: +{extra_added} net (bundled={len(bundled_only)}, "
            f"merged={len(traps)}) · sources: {' · '.join(srcs)}[/dim]"
        )

    console.print(
        f"[dim]Trap library: {len(traps)} traps · {len(idx.by_family)} families · "
        f"{composite_n} with composite attack chain in Pattern[/dim]"
        f"{extras_line}\n"
    )

    if args.list_only:
        console.print("[yellow][--list-only][/yellow] No eval run.")
        return 0

    # ── Wire harness LLM ──────────────────────────────────────────────
    harness_llm_name = args.harness_llm
    if not args.no_proxy:
        harness_llm_name = wire_proxy(args.proxy_url, args.harness_llm)
    if args.sequential:
        install_sequential_jury(args.per_call_timeout, verbose=True)
    install_think_stripper(harness_llm_name, verbose=True)

    # Install fallback LAST so it wraps everything (sequential semaphore +
    # think-stripper both run inside the fallback's _orig call). When the
    # stripped primary response is empty/garbled, fallback kicks in.
    fallback_tracker: dict | None = None
    if args.fallback_juror:
        fallback_tracker = install_fallback_juror(args.fallback_juror, verbose=True)

    # ── Build agent + trace collector ─────────────────────────────────
    raw_agent = make_agent_from_spec(spec, model=args.agent_model)
    context = make_context_from_spec(spec)
    collector = LiveTraceCollector(idx, verbose=args.verbose)
    wrapped_agent = collector.wrap_agent(raw_agent)

    # ── Build Harness ─────────────────────────────────────────────────
    # Same extra_traps + trap_packs as the TrapIndex above, so what the
    # collector visualizes is exactly what the planner/conductor can pick.
    harness = Harness(
        llm=harness_llm_name,
        turns=args.turns,
        consensus=args.consensus,
        seed=args.seed,
        context_budget_tokens=args.context_budget,
        extra_traps=extra_traps_resolved or None,
        trap_packs=trap_packs or None,
    )

    # ── Run ───────────────────────────────────────────────────────────
    console.print(Rule("[bold cyan]Evaluation start[/bold cyan]"))
    t0 = time.time()
    report = harness.evaluate(
        wrapped_agent,
        role=spec.role,
        business_case=spec.business_case,
        goal=spec.goal,
        context=context,
        on_event=collector.on_event,
    )
    elapsed = time.time() - t0

    # ── Final scorecard + coverage table ──────────────────────────────
    console.print(Rule("[bold green]Evaluation complete[/bold green]"))
    score_tbl = Table(show_header=True, header_style="bold")
    score_tbl.add_column("metric", style="dim")
    score_tbl.add_column("score", justify="right", style="bold")
    score_tbl.add_column("severity", style="dim")
    per_metric = getattr(report, "per_metric", {}) or {}
    severities = getattr(report, "severity", {}) or {}
    for m, s in per_metric.items():
        s_str = f"{s:.1f}" if isinstance(s, (int, float)) else str(s)
        score_tbl.add_row(m, s_str, str(severities.get(m, "")))
    score_tbl.add_row("[bold]final[/bold]", f"[bold]{report.final_score}[/bold]",
                      f"[bold]{report.certification}[/bold]")
    console.print(score_tbl)

    cov_tbl = Table(title="trap coverage this run", show_header=True, header_style="bold")
    cov_tbl.add_column("family", style="cyan")
    cov_tbl.add_column("turns", justify="right")
    cov_tbl.add_column("unique traps", justify="right")
    for fam in sorted(collector.families_seen, key=lambda f: -collector.families_seen[f]):
        n = collector.families_seen[fam]
        uniq = len({t for t in collector.traps_used if idx.by_name.get(t) and idx.by_name[t].family == fam})
        cov_tbl.add_row(fam, str(n), str(uniq))
    console.print(cov_tbl)

    console.print(f"\nWall time:                 {elapsed:.1f}s")
    console.print(f"Total turns:               {collector.turn_n}")
    console.print(f"Unique traps used:         {len(set(collector.traps_used))}")
    console.print(f"Families covered:          {len(collector.families_seen)} of {len(idx.by_family)}")
    console.print(f"Composite chains used:     {len(collector.composite_traps)} unique traps")
    console.print(f"Agent crashes:             {collector.crashes}")

    # Fallback usage breakdown (only if --fallback-juror was set)
    print_fallback_report(fallback_tracker)

    # ── Save report ───────────────────────────────────────────────────
    # BOTH the folder AND the files are self-describing. Example layout:
    #   results/
    #     live_customer_support_agent_AGENT-gpt-4.1_HARNESS-gemma-4-E4B-it-MLX-8bit_100turn_seed42_score7.8_20260526_201422/
    #         live_customer_support_agent_AGENT-gpt-4.1_HARNESS-gemma-4-E4B-it-MLX-8bit_100turn_seed42_score7.8.json
    #         live_customer_support_agent_AGENT-gpt-4.1_HARNESS-gemma-4-E4B-it-MLX-8bit_100turn_seed42_score7.8.md
    #
    # Folder = stem + timestamp suffix. Timestamp prevents collisions when
    # the same exact config (agent + harness + turns + seed + score) is run
    # twice. Files inside keep the descriptive stem so they remain
    # self-describing if anyone copies them out of the folder.
    #
    # Score tag is smart about silent juror failures: if per_metric has data,
    # we use the real score (e.g. `score7.8`). If per_metric is empty (juror
    # silently failed and the harness defaulted to 0.0/NOT_READY), we tag it
    # `scoreFAILED` so the bad run is unmistakable in any directory listing —
    # no more silently citing a 0.0 that wasn't a real evaluation.
    safe_agent_llm = args.agent_model.replace("/", "_").replace("@", "_")
    safe_harness   = args.harness_llm.replace("/", "_").replace("@", "_")

    fs = getattr(report, "final_score", None)
    pm = getattr(report, "per_metric", {}) or {}
    if not pm:
        # Silent juror failure — the 0.0 is a default, not a real score.
        score_tag = "scoreFAILED"
    elif isinstance(fs, (int, float)):
        score_tag = f"score{fs:.1f}"
    else:
        score_tag = "scoreNA"

    stem = (
        f"live_{spec.name}"
        f"_AGENT-{safe_agent_llm}"
        f"_HARNESS-{safe_harness}"
        f"_{args.turns}turn_seed{args.seed}"
        f"_{score_tag}"
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (DEFAULT_RESULTS_DIR / f"{stem}_{ts}")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    report.to_json(str(out_dir / f"{stem}.json"))
    report.to_markdown(str(out_dir / f"{stem}.md"))
    console.print(f"\n[bold]Report saved:[/bold]")
    console.print(f"  {out_dir / f'{stem}.json'}")
    console.print(f"  {out_dir / f'{stem}.md'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
