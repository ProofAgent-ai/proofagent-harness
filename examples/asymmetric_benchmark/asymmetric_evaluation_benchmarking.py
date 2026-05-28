#!/usr/bin/env python3
"""
asymmetric_evaluation_benchmarking.py — Asymmetric AI Agent Evaluation Benchmark
================================================================================

Self-contained benchmark distribution. See ./README.md for the full guide,
all CLI flags with examples, and the reproducibility notes for the paper.
Located at:
  examples/asymmetric_benchmark/asymmetric_evaluation_benchmarking.py

Paper-grade benchmarking sweep for the ProofAgent Harness asymmetric
evaluation regime: each of N small local Harness LLMs (served via an
LM-Studio / mlx proxy) evaluates each of M frontier-tier AI agents, with
a cross-family fallback juror automatically rescuing any juror call the
small primary LLM fails on.

The motivating result (cf. arXiv:2605.24134, §Asymmetric Evaluation):
    Frontier agents (GPT 5.5, Claude Opus 4.7) FAIL the harness with
    serious safety and manipulation-resistance issues under sustained
    multi-turn adversarial pressure — even when the juror is a small
    local model with no vendor alignment to the agent's LLM family.

This script reproduces and extends those cells across a configurable
sweep of small Harness LLMs.

KEY FEATURES
------------
  • Multi-cell sweep: N harness LLMs × M agents = N×M evaluation cells.
  • Cross-family fallback: if the primary (small local) juror returns
    empty / garbled / errored output, the same prompt is automatically
    retried with gpt-4.1-mini (default) or claude-haiku-4-5. Without
    this rescue, silent juror failures collapse the run's final_score
    to 0.0 with no findings.
  • Force-fallback mode: --force-fallback simulates a 100% primary
    failure rate so you can validate the fallback wiring before
    committing a real overnight sweep.
  • Compact per-turn logger: ONE line per turn (timestamp, trap, agent
    OK/crash/timeout, jury OK / fallback used). No rich panels.
  • Standalone experiment folder: every cell's report + a global
    summary CSV + trap-family coverage table land in one self-contained
    directory you can zip and ship with the paper.
  • Sequential by design: one juror call at a time per cell (the
    canonical asymmetric-regime constraint when running a single-
    threaded local proxy).
  • LM Studio model swap: optionally uses the `lms` CLI to load each
    harness LLM in turn between cells, or pauses for a manual swap.

OUTPUT LAYOUT
-------------
    results/asymmetric_<YYYYMMDD_HHMMSS>/
      ├── config.json                       # full run config snapshot
      ├── summary.csv                       # all cells, one row each
      ├── trap_coverage.md                  # family × harness-LLM matrix
      ├── cell_<harness>_x_<agent>_seed42_scoreX.X.json
      ├── cell_<harness>_x_<agent>_seed42_scoreX.X.md
      └── ... one .json + .md per cell ...

USAGE
-----
    # Default sweep: 5 small Harness LLMs × 5 bundled agents = 25 cells
    python examples/asymmetric_benchmark/asymmetric_evaluation_benchmarking.py

    # Custom subset
    python examples/asymmetric_benchmark/asymmetric_evaluation_benchmarking.py \\
        --agents customer_support_agent medical_triage_assistant \\
        --harness-llms gemma-4-E4B-it-MLX-8bit mlx-community/Qwen2.5-3B-Instruct-4bit \\
        --turns 8

    # Force the fallback on every juror call (validates wiring fast, no
    # local LLM needed for the test itself)
    python examples/asymmetric_benchmark/asymmetric_evaluation_benchmarking.py \\
        --agents customer_support_agent \\
        --force-fallback --turns 4

    # Wiring check — no API calls, just print the matrix
    python examples/asymmetric_benchmark/asymmetric_evaluation_benchmarking.py --list-only

REQUIRED ENVIRONMENT
--------------------
    OPENAI_API_KEY            — for the gpt-4.1 agent (default)
    ANTHROPIC_API_KEY         — for claude-* agent or --fallback-llm anthropic/*
    LM Studio                 — running locally with the harness LLM loaded
                                (or use --no-proxy with a cloud harness LLM
                                 like anthropic/claude-haiku-4-5 for testing)

LICENSE
-------
Apache-2.0. Part of the ProofAgent Harness reference distribution.
Cite: Bousetouane, F. (2026). ProofAgent Harness: Open Infrastructure for
Adversarial Evaluation of AI Agents. arXiv:2605.24134.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from proofagent_harness import (
    AgentContext,
    AgentResponse,
    Event,
    Harness,
    TrapIndex,
    load_traps,
)

# ═════════════════════════════════════════════════════════════════════════════
#  Paths + agent factory bootstrap (shared with 09 / 11)
# ═════════════════════════════════════════════════════════════════════════════

# Paths — this script lives at
#   examples/asymmetric_benchmark/asymmetric_evaluation_benchmarking.py
# so the agents/ directory is one level up at examples/agents/, and the
# default results directory is colocated with this script (every benchmark
# run lands under ./results/<timestamp>/ unless --output-dir overrides).
_HERE = Path(__file__).resolve().parent
AGENTS_DIR = _HERE.parent / "agents"
DEFAULT_RESULTS_DIR = _HERE / "results"

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


# ═════════════════════════════════════════════════════════════════════════════
#  Defaults — sensible, paper-friendly. Override via CLI.
# ═════════════════════════════════════════════════════════════════════════════

# Five small quantized models commonly available via LM Studio / mlx-community.
# Each entry is the model identifier as the proxy serves it. Swap or add
# your own via --harness-llms.
DEFAULT_HARNESS_LLMS: list[str] = [
    "gemma-4-E4B-it-MLX-8bit",
    "mlx-community/Llama-3.2-3B-Instruct-4bit",
    "mlx-community/Qwen2.5-3B-Instruct-4bit",
    "mlx-community/Phi-3.5-mini-instruct-4bit",
    "mlx-community/SmolLM2-1.7B-Instruct-bf16",
]

# Default fallback for cross-family juror validation. Both options are
# available; gpt-4.1-mini is cheapest, claude-haiku is slightly cheaper
# but needs ANTHROPIC_API_KEY. Picked the OpenAI default to minimize key
# dependencies (the agent already needs OPENAI_API_KEY for gpt-4.1).
DEFAULT_FALLBACK_JUROR = "gpt-4.1-mini"

# Frontier agent default. The asymmetric regime evaluates a frontier agent
# with a small juror — gpt-4.1 is the canonical "frontier" choice for cross-
# family testing (since fallback is gpt-4.1-mini, primary juror is local).
DEFAULT_AGENT_MODEL = "gpt-4.1"


def discover_bundled_agents() -> list[str]:
    """Return short names of bundled agent specs under examples/agents/."""
    return sorted(p.stem for p in AGENTS_DIR.glob("*.json"))


def resolve_agent_spec_path(name_or_path: str) -> Path:
    """Accept short name (`customer_support_agent`), filename (`...json`), or
    absolute path. Same resolver as 09 / 11."""
    p = Path(name_or_path).expanduser()
    if p.is_absolute() and p.exists():
        return p
    for c in (
        AGENTS_DIR / name_or_path,
        AGENTS_DIR / f"{name_or_path}.json",
        AGENTS_DIR / Path(name_or_path).name,
    ):
        if c.exists():
            return c
    raise SystemExit(
        f"--agent {name_or_path!r} not found. Available: "
        f"{discover_bundled_agents()}. Or pass an absolute path."
    )


# ═════════════════════════════════════════════════════════════════════════════
#  LiteLLM wrappers — proxy routing, sequential semaphore, think-tag strip,
#  cross-family fallback, force-fallback test injector.
#  Composable: each wrap composes on top of the previous, so install order
#  determines outer-vs-inner. Recommended order for an asymmetric cell:
#    sequential → think-stripper → fallback → (optional) force-fallback
# ═════════════════════════════════════════════════════════════════════════════


def wire_proxy(proxy_url: str, harness_llm: str) -> str:
    """Set OPENAI_BASE_URL so LiteLLM routes the harness LLM to a local
    proxy. Returns the (possibly prefixed) model name to pass into
    `Harness(llm=...)`."""
    os.environ["OPENAI_BASE_URL"] = proxy_url
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "not-required-for-local-proxy"
    if not harness_llm.startswith(
        ("openai/", "anthropic/", "gemini/", "bedrock/", "ollama/", "groq/")
    ):
        return f"openai/{harness_llm}"
    return harness_llm


def install_sequential_jury(per_call_timeout: int) -> None:
    """Serialize juror calls under an asyncio semaphore — required for any
    single-threaded local proxy."""
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


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_TRAIL = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_REASONING_HINTS = (
    "gemma", "deepseek-r1", "deepseek_r1", "qwq", "openai/o1", "openai/o3",
    "o1-", "o3-", "qwen", "smollm",
)


def install_think_stripper(harness_llm: str) -> bool:
    """Strip <think>…</think> blocks from LiteLLM responses so the juror
    JSON parser doesn't choke on them. Most small reasoning models (Gemma,
    DeepSeek-R1, QwQ, Qwen, SmolLM) emit them."""
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
        except Exception:  # never crash the response path
            pass
        return r

    litellm.acompletion = _patched
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  v0.4.2: native fallback support
#
#  Earlier versions of this script monkey-patched litellm.acompletion to
#  intercept failures and route to a cross-family fallback. That entire
#  layer is now built into the proofagent-harness library itself:
#
#      Harness(llm="openai/gemma-...",
#              fallback_llm="anthropic/claude-haiku-4-5-...")
#
#  The library handles JSON-parse detection, empty-content detection,
#  exception routing, per-source token tracking, and the [fallback]
#  progress line — all without any litellm patching at this layer. See
#  CHANGELOG.md for the full v0.4.2 rationale + design.
#
#  The `LiveLLMStats` snapshot below pulls the per-source stats from the
#  Harness's LLM instance after the eval completes, so the rest of this
#  script (CellResult, summary.csv, README.md) keeps the same shape.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LiveLLMStats:
    """Per-source LLM call stats for one cell. Populated by reading
    `harness.llm.*` counters after evaluation finishes. The same fields as
    the pre-v0.4.2 FallbackTracker so CellResult / summary.csv stay shape-
    compatible."""
    fallback_model: str
    primary_calls: int = 0
    primary_prompt_tokens: int = 0
    primary_completion_tokens: int = 0
    fallback_calls: int = 0
    fallback_prompt_tokens: int = 0
    fallback_completion_tokens: int = 0

    @classmethod
    def from_llm(cls, llm: Any, fallback_model: str) -> "LiveLLMStats":
        """Snapshot a Harness LLM's per-source counters."""
        return cls(
            fallback_model=fallback_model,
            primary_calls=int(getattr(llm, "primary_call_count", 0)),
            primary_prompt_tokens=int(getattr(llm, "primary_prompt_tokens", 0)),
            primary_completion_tokens=int(getattr(llm, "primary_completion_tokens", 0)),
            fallback_calls=int(getattr(llm, "fallback_call_count", 0)),
            fallback_prompt_tokens=int(getattr(llm, "fallback_prompt_tokens", 0)),
            fallback_completion_tokens=int(getattr(llm, "fallback_completion_tokens", 0)),
        )


def install_force_fallback(primary_model: str) -> None:
    """Make EVERY juror call that targets `primary_model` return empty.
    Calls to OTHER models (i.e. fallback retries from the v0.4.2 native
    library-side fallback) pass through to the real LiteLLM call.

    Used with `--force-fallback` to validate the rescue wiring without
    waiting for the small local LLM to actually choke on JSON. The flow:

        harness.complete_json → LLM._raw_complete → litellm.acompletion
                                                      = _force_empty
                                                      → returns ""
                              → primary "" fails JSON parse
                              → library fires fallback
                              → fallback.complete_json → _raw_complete
                                → litellm.acompletion = _force_empty
                                → model name != primary_model
                                → passes through to real OpenAI/Anthropic
                              → fallback returns valid JSON
                              → harness uses it

    Identifies "primary" by EXACT model-name match — works regardless of
    how the fallback is routed (Anthropic, OpenAI, Gemini, Bedrock)."""
    import litellm
    from types import SimpleNamespace

    _orig = litellm.acompletion

    async def _force_empty(*args, **kwargs):
        if kwargs.get("model", "") != primary_model:
            # Fallback retry — pass through to the real network call.
            return await _orig(*args, **kwargs)
        # Primary path — return a structured empty response so the
        # library-side fallback's JSON-parse check fails and triggers retry.
        msg = SimpleNamespace(content="", role="assistant", tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop", index=0)
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        return SimpleNamespace(choices=[choice], usage=usage, model=primary_model)

    litellm.acompletion = _force_empty


# ═════════════════════════════════════════════════════════════════════════════
#  Compact per-turn logger — one line per significant event.
#  Replaces the rich panels of 11_live_trace_evaluation.py with a single-line
#  structured log designed for sweeps + debugging. NOT a TUI, just stdout.
# ═════════════════════════════════════════════════════════════════════════════


class CompactCellLogger:
    """Emits one log line per turn-start, one line per juror vote during the
    jury phase, and one summary line at cell end. Per-juror visibility matters
    on long runs — turns=50 / debate consensus puts ~60 juror calls between
    the last turn and the scorecard (10-60 min of otherwise-silent wall time)."""

    TRAP_RE = re.compile(r"trap:\s*(\S+)")
    SCORE_RE = re.compile(r"scored\s+([\d.]+)")

    def __init__(self, cell_label: str, trap_index: TrapIndex, llm: Any):
        self.label = cell_label
        self.idx = trap_index
        # The Harness's LLM instance — the per-source counters live here
        # in v0.4.2 (previously on a FallbackTracker we owned). Snapshotted
        # per juror to detect via=primary|fallback and per-juror token cost.
        self.llm = llm
        self.turn = 0
        self.current_trap_name: str | None = None
        self.turn_t0: float = 0.0
        self.crashes = 0
        self.turns_seen: list[dict[str, Any]] = []
        # ── jury-phase state ──
        self.juror_idx = 0
        self.juror_t0: float = 0.0
        self.round_idx = 0
        self.round_t0: float = 0.0
        self.round_start_idx = 0
        self.round_start_fb = 0
        self.last_fb_seen = 0
        # Per-juror token-delta tracking (so each progress line shows the
        # tokens THAT specific juror call consumed — useful for spotting
        # which trap families generate prompt explosions).
        self.last_primary_pt_seen = 0
        self.last_primary_ct_seen = 0
        self.last_fallback_pt_seen = 0
        self.last_fallback_ct_seen = 0

    def on_event(self, ev: Event) -> None:
        if ev.type == "turn_start" and ev.detail:
            m = self.TRAP_RE.search(ev.detail)
            if m:
                self.current_trap_name = m.group(1)
            self.turn = ev.turn or (self.turn + 1)
            self.turn_t0 = time.time()
            # Log line is emitted from wrap_agent's response handler instead,
            # so we have a single line per turn carrying both trap + outcome.
        elif ev.type == "jury_round_start":
            self.round_idx += 1
            self.round_t0 = time.time()
            self.round_start_idx = self.juror_idx
            self.round_start_fb = self.llm.fallback_call_count
            self.last_fb_seen = self.llm.fallback_call_count
            self.last_primary_pt_seen = self.llm.primary_prompt_tokens
            self.last_primary_ct_seen = self.llm.primary_completion_tokens
            self.last_fallback_pt_seen = self.llm.fallback_prompt_tokens
            self.last_fallback_ct_seen = self.llm.fallback_completion_tokens
            self.juror_t0 = time.time()
            ts = datetime.now().strftime("%H:%M:%S")
            detail = ev.detail or ""
            print(
                f"  [{ts}] ── jury round {self.round_idx} starting"
                f"{' (' + detail + ')' if detail else ''} ──",
                flush=True,
            )
        elif ev.type == "juror_scored":
            self.juror_idx += 1
            elapsed = time.time() - self.juror_t0
            # Was THIS call rescued by the fallback? Snapshot-diff the tracker.
            # Reliable because install_sequential_jury forces serialized
            # litellm.acompletion calls — no parallel jurors to confuse the
            # counter delta.
            delta_fb = self.llm.fallback_call_count - self.last_fb_seen
            via = "fallback" if delta_fb > 0 else "primary"
            self.last_fb_seen = self.llm.fallback_call_count

            # Per-juror token delta for the source that handled this call.
            if via == "primary":
                dpt = self.llm.primary_prompt_tokens - self.last_primary_pt_seen
                dct = self.llm.primary_completion_tokens - self.last_primary_ct_seen
            else:
                dpt = self.llm.fallback_prompt_tokens - self.last_fallback_pt_seen
                dct = self.llm.fallback_completion_tokens - self.last_fallback_ct_seen
            self.last_primary_pt_seen = self.llm.primary_prompt_tokens
            self.last_primary_ct_seen = self.llm.primary_completion_tokens
            self.last_fallback_pt_seen = self.llm.fallback_prompt_tokens
            self.last_fallback_ct_seen = self.llm.fallback_completion_tokens

            score_m = self.SCORE_RE.search(ev.detail or "")
            score_s = score_m.group(1) if score_m else "?.?"
            payload = ev.payload or {}
            persona = str(payload.get("persona", "?"))
            metric = ev.metric or "?"
            metric_short = metric if len(metric) <= 24 else metric[:21] + "..."
            ts = datetime.now().strftime("%H:%M:%S")
            tok_str = f"{dpt:>5d}p+{dct:>4d}c" if (dpt + dct) > 0 else "  no-usage"
            print(
                f"  [{ts}] R{self.round_idx}.J{self.juror_idx:02d}  "
                f"metric={metric_short:24s}  persona={persona:10s}  "
                f"score={score_s:>4s}  ({elapsed:5.1f}s)  via={via:8s}  tok={tok_str}",
                flush=True,
            )
            self.juror_t0 = time.time()
        elif ev.type == "jury_round_end":
            round_votes = self.juror_idx - self.round_start_idx
            round_fb = self.llm.fallback_call_count - self.round_start_fb
            round_elapsed = time.time() - self.round_t0
            # Round-level token split — running totals captured at round_start
            # vs current tracker. round_start_* were primed in jury_round_start.
            round_primary_tok = (
                (self.llm.primary_prompt_tokens + self.llm.primary_completion_tokens)
                - 0  # since the primary cumulative starts at 0 only on cell start;
                # for the per-round delta we'd need round-start snapshots, but the
                # simpler thing is to show the cell-cumulative split here.
            )
            round_fallback_tok = (
                self.llm.fallback_prompt_tokens + self.llm.fallback_completion_tokens
            )
            tot = round_primary_tok + round_fallback_tok
            split_str = (
                f" · cell tok so far: local={round_primary_tok / 1000:.1f}k "
                f"({(round_primary_tok / tot * 100) if tot else 0:.0f}%) "
                f"fb={round_fallback_tok / 1000:.1f}k "
                f"({(round_fallback_tok / tot * 100) if tot else 0:.0f}%)"
            )
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"  [{ts}] ── jury round {self.round_idx} done: "
                f"{round_votes} votes, {round_fb} via fallback "
                f"({round_elapsed:.1f}s){split_str} ──",
                flush=True,
            )

    def wrap_agent(
        self, agent: Callable[[str], AgentResponse]
    ) -> Callable[[str], AgentResponse]:
        def wrapped(message: str) -> AgentResponse:
            try:
                resp = agent(message)
                self._log_turn(outcome="ok", elapsed=time.time() - self.turn_t0)
                return resp
            except Exception as exc:
                self.crashes += 1
                self._log_turn(
                    outcome=f"crash:{type(exc).__name__}",
                    elapsed=time.time() - self.turn_t0,
                )
                raise

        return wrapped

    def _log_turn(self, outcome: str, elapsed: float) -> None:
        # NOTE: juror calls do NOT happen during the per-turn agent phase —
        # they all run AFTER the conductor finishes its N turns, in the
        # consensus phase. So per-turn lines intentionally do NOT carry a
        # 'jury=' tag. Aggregate fallback stats land in the end-of-cell
        # summary line + summary.csv.
        trap = self.current_trap_name or "?"
        ts = datetime.now().strftime("%H:%M:%S")
        trap_display = trap if len(trap) <= 38 else trap[:35] + "…"
        print(
            f"  [{ts}] T{self.turn:02d}  trap={trap_display:38s}"
            f"  agent {outcome:14s}  ({elapsed:5.1f}s)",
            flush=True,
        )
        self.turns_seen.append(
            {
                "turn": self.turn,
                "trap": trap,
                "agent_outcome": outcome,
                "agent_elapsed_s": round(elapsed, 2),
            }
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Optional LM Studio model swap via the `lms` CLI.
#  Best-effort — if `lms` isn't on PATH we degrade to an interactive prompt.
# ═════════════════════════════════════════════════════════════════════════════


def _have_lms_cli() -> bool:
    return shutil.which("lms") is not None


def swap_lm_studio_model(model_id: str, *, no_swap: bool, interactive: bool) -> None:
    """Try to load `model_id` in LM Studio for the next cell.
    - --no-swap: assume model is already loaded; do nothing.
    - lms CLI present: programmatically unload all + load model_id.
    - else: pause and wait for the user to load it manually.
    """
    if no_swap:
        print(f"  [swap] skipped (--no-swap) — assuming {model_id} is loaded.", flush=True)
        return
    if _have_lms_cli():
        try:
            print(f"  [swap] lms unload --all", flush=True)
            subprocess.run(
                ["lms", "unload", "--all"], check=False,
                capture_output=True, text=True, timeout=60,
            )
            print(f"  [swap] lms load {model_id}", flush=True)
            r = subprocess.run(
                ["lms", "load", model_id], check=False,
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode != 0:
                print(
                    f"  [swap] lms load failed (rc={r.returncode}). stderr:\n"
                    f"      {r.stderr.strip()[:300]}",
                    flush=True,
                )
                if interactive:
                    input(f"  [swap] load {model_id!r} manually in LM Studio, then ENTER…")
            else:
                print(f"  [swap] {model_id} loaded.", flush=True)
        except Exception as e:
            print(f"  [swap] lms invocation errored: {type(e).__name__}: {e}", flush=True)
            if interactive:
                input(f"  [swap] load {model_id!r} manually in LM Studio, then ENTER…")
    elif interactive:
        input(
            f"  [swap] `lms` CLI not found. Load {model_id!r} in LM Studio, "
            f"then press ENTER…"
        )
    else:
        print(
            f"  [swap] WARN: `lms` not on PATH and not interactive. "
            f"Assuming {model_id} is already loaded.",
            flush=True,
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Single-cell runner — one Harness LLM evaluating one Agent.
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class CellResult:
    harness_llm: str
    agent_name: str
    agent_model: str
    seed: int
    turns: int
    consensus: str
    final_score: float | None
    certification: str | None
    per_metric: dict[str, float] = field(default_factory=dict)
    severity: dict[str, str] = field(default_factory=dict)
    findings_count: int = 0
    wall_time_s: float = 0.0
    primary_juror_calls: int = 0
    primary_juror_empty: int = 0
    primary_juror_errors: int = 0
    primary_juror_timeouts: int = 0
    primary_prompt_tokens: int = 0
    primary_completion_tokens: int = 0
    fallback_juror_calls: int = 0
    fallback_juror_errors: int = 0
    fallback_prompt_tokens: int = 0
    fallback_completion_tokens: int = 0
    fallback_rate: float = 0.0
    # Asymmetric-cost metrics — derived from the per-source token totals.
    # local_token_share = primary_tokens / (primary_tokens + fallback_tokens)
    # A higher share means more juror work was carried by the cheap local
    # model and less was offloaded to the cross-family fallback. This is
    # the paper's headline cost ratio.
    local_token_share: float = 0.0
    fallback_token_share: float = 0.0
    agent_crashes: int = 0
    unique_traps_fired: int = 0
    families_fired: int = 0
    traps_by_family: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    cell_dir: str | None = None
    report_json_path: str | None = None
    report_md_path: str | None = None


def run_single_cell(
    *,
    out_dir: Path,
    harness_llm_name: str,
    agent_spec_path: Path,
    agent_model: str,
    fallback_llm: str,
    turns: int,
    consensus: str,
    seed: int,
    context_budget_tokens: int,
    per_call_timeout: int,
    juror_call_timeout: int,
    max_tokens: int,
    proxy_url: str | None,
    extra_traps: list[str] | None,
    force_fallback: bool,
    trap_index: TrapIndex,
) -> CellResult:
    """Run one Harness LLM × one Agent cell. Returns a CellResult and writes
    the per-cell JSON + Markdown report files."""
    spec = load_agent_spec(agent_spec_path)
    cell_label = f"{_short_model_name(harness_llm_name)} × {spec.name}"

    # ── Wire the LiteLLM stack: proxy → think-strip → fallback → force ──
    routed_harness_llm = harness_llm_name
    if proxy_url:
        routed_harness_llm = wire_proxy(proxy_url, harness_llm_name)
    # --juror-call-timeout (if set) tightens the per-call litellm timeout.
    # The litellm-native timeout cleanly raises httpx.ReadTimeout into our
    # _with_fallback exception handler, which routes to the fallback juror.
    # (We deliberately do NOT layer asyncio.wait_for on top — it conflicts
    # with the install_sequential_jury semaphore and leaves coroutines
    # half-completed.)
    effective_timeout = per_call_timeout
    if juror_call_timeout and 0 < juror_call_timeout < per_call_timeout:
        effective_timeout = juror_call_timeout
    install_sequential_jury(effective_timeout)
    install_think_stripper(routed_harness_llm)
    # ── v0.4.2: fallback is now NATIVE to the Harness library ────────────
    # The asymmetric design (small local primary + cross-family rescue) is
    # the supported public API. We just pass fallback_llm= to Harness(...)
    # and the library handles the rest: JSON-parse detection, empty-content
    # detection, exception routing, per-source token tracking, the
    # [fallback] progress line, and the on_event 'fallback_triggered'
    # signal. No litellm monkey-patching at this layer.
    #
    # --force-fallback IS still useful for validating the rescue wiring
    # without waiting for the small primary to actually choke on JSON. We
    # install it BEFORE constructing Harness so it patches litellm.acompletion
    # first; the library-side fallback then wraps it cleanly.
    if force_fallback:
        install_force_fallback(primary_model=routed_harness_llm)

    # ── Build the Harness with native fallback_llm + max_tokens support ──
    # max_tokens is the per-call OUTPUT cap (not context window). At turns=50
    # with debate consensus, each juror call emits ~4000 tokens of audit JSON
    # (50-entry per_turn_audit array + reasoning). Library default 8192 fits
    # this comfortably for all common fallback models (Haiku 8K, GPT-4.1-mini
    # 32K). The benchmark script's --max-tokens flag overrides this default.
    harness = Harness(
        llm=routed_harness_llm,
        fallback_llm=fallback_llm,
        max_tokens=max_tokens,
        turns=turns,
        consensus=consensus,
        seed=seed,
        context_budget_tokens=context_budget_tokens,
        extra_traps=extra_traps or None,
    )

    # ── Build the agent + the compact logger ──
    raw_agent = make_agent_from_spec(spec, model=agent_model)
    context = make_context_from_spec(spec)
    # Logger reads per-source counters directly off the Harness's primary
    # LLM instance — the same LLM the library updates on every call.
    logger = CompactCellLogger(cell_label, trap_index, harness.llm)
    wrapped_agent = logger.wrap_agent(raw_agent)

    print(f"  ╭─ CELL {cell_label}  (seed={seed}, turns={turns}, consensus={consensus})", flush=True)
    if force_fallback:
        print("  │  [force-fallback] all primary juror calls will be forced empty.", flush=True)

    # ── Run the evaluation ──
    t0 = time.time()
    error_msg: str | None = None
    report = None
    try:
        report = harness.evaluate(
            wrapped_agent,
            role=spec.role,
            business_case=spec.business_case,
            goal=spec.goal,
            context=context,
            on_event=logger.on_event,
        )
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"  │  [ERROR] {error_msg}", flush=True)
    elapsed = time.time() - t0

    # ── Snapshot per-source LLM stats (replaces the old fallback_tracker) ──
    fallback_tracker = LiveLLMStats.from_llm(harness.llm, fallback_model=fallback_llm)

    # ── Build the CellResult ──
    fs = getattr(report, "final_score", None) if report else None
    pm = getattr(report, "per_metric", {}) if report else {}
    pm = pm or {}
    sev = getattr(report, "severity", {}) if report else {}
    sev = sev or {}
    cert = getattr(report, "certification", None) if report else None
    cert_str = cert.value if hasattr(cert, "value") else (str(cert) if cert else None)
    findings_n = len(getattr(report, "findings", []) or []) if report else 0

    # Trap firing stats from the compact logger's turn list
    trap_counter = Counter(t["trap"] for t in logger.turns_seen if t.get("trap"))
    family_counter: dict[str, int] = defaultdict(int)
    for trap_name, n in trap_counter.items():
        t = trap_index.by_name.get(trap_name)
        if t is not None:
            family_counter[t.family] += n

    fb_total = fallback_tracker.primary_calls
    fb_rate = (
        (fallback_tracker.fallback_calls / fb_total) * 100.0 if fb_total else 0.0
    )
    # Asymmetric-cost: how much juror work (in tokens) was carried by the
    # cheap local model vs offloaded to the cross-family fallback. High
    # local_token_share = healthy asymmetric design; low = the small model
    # was overwhelmed and the fallback is doing most of the work.
    primary_tok = (
        fallback_tracker.primary_prompt_tokens
        + fallback_tracker.primary_completion_tokens
    )
    fallback_tok = (
        fallback_tracker.fallback_prompt_tokens
        + fallback_tracker.fallback_completion_tokens
    )
    total_tok = primary_tok + fallback_tok
    local_share = (primary_tok / total_tok) if total_tok else 0.0
    fb_share = (fallback_tok / total_tok) if total_tok else 0.0

    score_tag = (
        "scoreFAILED" if (not pm)
        else (f"score{fs:.1f}" if isinstance(fs, (int, float)) else "scoreNA")
    )
    cell_stem = (
        f"cell_{_safe(_short_model_name(harness_llm_name))}"
        f"_x_{spec.name}"
        f"_seed{seed}_{score_tag}"
    )
    cell_dir = out_dir / cell_stem
    cell_dir.mkdir(parents=True, exist_ok=True)
    j_path = cell_dir / f"{cell_stem}.json"
    m_path = cell_dir / f"{cell_stem}.md"
    if report is not None:
        try:
            report.to_json(str(j_path))
            report.to_markdown(str(m_path))
        except Exception as e:
            print(f"  │  [WARN] report serialization failed: {e}", flush=True)

    # Per-cell turn log (lightweight, separate from the harness's own report)
    (cell_dir / "turns.json").write_text(
        json.dumps({"label": cell_label, "turns": logger.turns_seen}, indent=2)
    )

    cell = CellResult(
        harness_llm=harness_llm_name,
        agent_name=spec.name,
        agent_model=agent_model,
        seed=seed,
        turns=turns,
        consensus=consensus,
        final_score=float(fs) if isinstance(fs, (int, float)) else None,
        certification=cert_str,
        per_metric={k: float(v) for k, v in pm.items() if isinstance(v, (int, float))},
        severity={k: str(v) for k, v in sev.items()},
        findings_count=findings_n,
        wall_time_s=round(elapsed, 1),
        primary_juror_calls=fallback_tracker.primary_calls,
        # primary_juror_empty / primary_juror_errors / primary_juror_timeouts
        # / fallback_juror_errors used to be tracked by the monkey-patched
        # fallback wrapper. The v0.4.2 native library doesn't expose these
        # breakdowns yet — kept as 0 for CSV-shape compatibility. The
        # information is captured in stdout `[fallback]` log lines + the
        # Report.fallback_rate aggregate (= breakdowns summed).
        primary_juror_empty=0,
        primary_juror_errors=0,
        primary_juror_timeouts=0,
        primary_prompt_tokens=fallback_tracker.primary_prompt_tokens,
        primary_completion_tokens=fallback_tracker.primary_completion_tokens,
        fallback_juror_calls=fallback_tracker.fallback_calls,
        fallback_juror_errors=0,
        fallback_prompt_tokens=fallback_tracker.fallback_prompt_tokens,
        fallback_completion_tokens=fallback_tracker.fallback_completion_tokens,
        fallback_rate=round(fb_rate, 1),
        local_token_share=round(local_share, 4),
        fallback_token_share=round(fb_share, 4),
        agent_crashes=logger.crashes,
        unique_traps_fired=len(trap_counter),
        families_fired=len(family_counter),
        traps_by_family=dict(family_counter),
        error=error_msg,
        cell_dir=str(cell_dir),
        report_json_path=str(j_path) if report else None,
        report_md_path=str(m_path) if report else None,
    )

    # ── Compact summary line ──
    score_str = f"{cell.final_score:.1f}/10" if cell.final_score is not None else "—"
    timeout_tag = (
        f" · timeouts={cell.primary_juror_timeouts}"
        if cell.primary_juror_timeouts else ""
    )

    def _ktok(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    primary_tok_str = (
        f"{_ktok(cell.primary_prompt_tokens)}p+"
        f"{_ktok(cell.primary_completion_tokens)}c"
    )
    fb_tok_str = (
        f"{_ktok(cell.fallback_prompt_tokens)}p+"
        f"{_ktok(cell.fallback_completion_tokens)}c"
    )
    asym_tag = (
        f" · tok: local={primary_tok_str} ({cell.local_token_share*100:.0f}%) "
        f"· fb={fb_tok_str} ({cell.fallback_token_share*100:.0f}%)"
    )
    print(
        f"  ╰─ done · score={score_str} ({cell.certification or 'NA'}) · "
        f"jury_fallback={cell.fallback_juror_calls}/{cell.primary_juror_calls} "
        f"({cell.fallback_rate:.0f}%){timeout_tag}{asym_tag} · crashes={cell.agent_crashes} · "
        f"wall={cell.wall_time_s:.0f}s",
        flush=True,
    )
    return cell


# ═════════════════════════════════════════════════════════════════════════════
#  Sweep + reporting
# ═════════════════════════════════════════════════════════════════════════════


def write_summary_csv(out_dir: Path, cells: list[CellResult]) -> Path:
    """Per-cell summary CSV — one row per cell."""
    fields = [
        "harness_llm", "agent_name", "agent_model", "seed", "turns", "consensus",
        "final_score", "certification", "findings_count", "wall_time_s",
        "primary_juror_calls", "primary_juror_empty", "primary_juror_errors",
        "primary_juror_timeouts",
        "primary_prompt_tokens", "primary_completion_tokens",
        "fallback_juror_calls", "fallback_juror_errors",
        "fallback_prompt_tokens", "fallback_completion_tokens",
        "fallback_rate", "local_token_share", "fallback_token_share",
        "agent_crashes", "unique_traps_fired", "families_fired", "error",
        # per-metric flattened
        "metric_task_success", "metric_hallucination_resistance",
        "metric_safety", "metric_instruction_following",
        "metric_manipulation_resistance",
    ]
    path = out_dir / "summary.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in cells:
            row = {k: getattr(c, k, "") for k in fields if not k.startswith("metric_")}
            for m in ("task_success", "hallucination_resistance", "safety",
                      "instruction_following", "manipulation_resistance"):
                row[f"metric_{m}"] = c.per_metric.get(m, "")
            w.writerow(row)
    return path


def write_trap_coverage(out_dir: Path, cells: list[CellResult], trap_index: TrapIndex) -> Path:
    """Markdown table: trap family × harness LLM, counts of traps fired in
    each cell, grouped by harness LLM."""
    # Total traps per family in the library
    family_totals: dict[str, int] = defaultdict(int)
    for fam in trap_index.by_family:
        family_totals[fam] = len(trap_index.by_family[fam])

    families = sorted(family_totals.keys())
    harness_llms = sorted({c.harness_llm for c in cells})
    short = {h: _short_model_name(h) for h in harness_llms}

    # Aggregate per harness LLM across its cells (sum the family counts)
    per_llm_family: dict[str, dict[str, int]] = {
        h: defaultdict(int) for h in harness_llms
    }
    cells_per_llm: dict[str, int] = defaultdict(int)
    for c in cells:
        cells_per_llm[c.harness_llm] += 1
        for fam, n in c.traps_by_family.items():
            per_llm_family[c.harness_llm][fam] += n

    lines: list[str] = []
    lines.append("# Trap-family coverage matrix")
    lines.append("")
    lines.append(
        "Counts of trap firings (turns) per family, aggregated across all "
        "agents evaluated under each Harness LLM. The **Library** column is "
        "the total number of distinct traps available in that family. Higher "
        "numbers per Harness LLM column mean that LLM-as-juror cell drove "
        "more turns into that family — useful for spotting planner / "
        "trap-selection bias across small models."
    )
    lines.append("")
    header = "| Family | Library | " + " | ".join(short[h] for h in harness_llms) + " |"
    sep = "|---|---:|" + ":---:|" * len(harness_llms)
    lines.append(header)
    lines.append(sep)
    for fam in families:
        row = [fam, str(family_totals[fam])]
        for h in harness_llms:
            row.append(str(per_llm_family[h].get(fam, 0)))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(f"_{len(cells)} cells across {len(harness_llms)} Harness LLMs × "
                 f"{len({c.agent_name for c in cells})} agents._")
    lines.append("")
    lines.append("## Cells per Harness LLM")
    lines.append("")
    lines.append("| Harness LLM | Cells | Mean fallback rate | Mean final_score |")
    lines.append("|---|---:|---:|---:|")
    for h in harness_llms:
        sub = [c for c in cells if c.harness_llm == h]
        mean_fb = (sum(c.fallback_rate for c in sub) / len(sub)) if sub else 0.0
        scores = [c.final_score for c in sub if c.final_score is not None]
        mean_score = (sum(scores) / len(scores)) if scores else None
        ms = f"{mean_score:.2f}" if mean_score is not None else "—"
        lines.append(f"| `{h}` | {len(sub)} | {mean_fb:.1f}% | {ms} |")

    path = out_dir / "trap_coverage.md"
    path.write_text("\n".join(lines))
    return path


def write_overview_md(out_dir: Path, cells: list[CellResult], cfg: dict[str, Any]) -> Path:
    """Top-level human-readable README for the experiment folder."""
    lines: list[str] = []
    lines.append("# Asymmetric benchmarking experiment")
    lines.append("")
    lines.append(f"_Started:_ `{cfg.get('started_at')}`")
    lines.append(f"_Total cells:_ **{len(cells)}**  ({cfg.get('n_harness_llms')} Harness LLMs × {cfg.get('n_agents')} agents)")
    lines.append(f"_Sequential:_ yes · _Consensus:_ {cfg.get('consensus')} · _Turns/cell:_ {cfg.get('turns')} · _Seed:_ {cfg.get('seed')}")
    lines.append(f"_Fallback LLM:  _ `{cfg.get('fallback_llm')}` · _Force-fallback mode:_ {'yes' if cfg.get('force_fallback') else 'no'}")
    lines.append("")
    lines.append("## Per-cell summary")
    lines.append("")
    lines.append(
        "| Harness LLM | Agent | Score | Cert | Fallback | Local tok | FB tok | Local% | Crashes | Wall |"
    )
    lines.append("|---|---|---:|---|---:|---:|---:|---:|---:|---:|")
    sum_local_tok = 0
    sum_fb_tok = 0
    for c in cells:
        score = f"{c.final_score:.1f}" if c.final_score is not None else "—"
        cert = c.certification or "NA"
        fb_calls = (
            f"{c.fallback_juror_calls}/{c.primary_juror_calls} ({c.fallback_rate:.0f}%)"
            if c.primary_juror_calls else "—"
        )
        local_tok = c.primary_prompt_tokens + c.primary_completion_tokens
        fb_tok = c.fallback_prompt_tokens + c.fallback_completion_tokens
        sum_local_tok += local_tok
        sum_fb_tok += fb_tok
        lines.append(
            f"| `{_short_model_name(c.harness_llm)}` | {c.agent_name} | "
            f"{score} | {cert} | {fb_calls} | {local_tok / 1000:.1f}k | "
            f"{fb_tok / 1000:.1f}k | {c.local_token_share * 100:.1f}% | "
            f"{c.agent_crashes} | {c.wall_time_s:.0f}s |"
        )
    lines.append("")
    sweep_tot = sum_local_tok + sum_fb_tok
    sweep_local_pct = (sum_local_tok / sweep_tot * 100.0) if sweep_tot else 0.0
    sweep_fb_pct = (sum_fb_tok / sweep_tot * 100.0) if sweep_tot else 0.0
    lines.append("## Asymmetric-cost headline")
    lines.append("")
    lines.append(
        f"Across this sweep, **{sweep_local_pct:.1f}%** of juror tokens "
        f"({sum_local_tok / 1000:.1f}k) were served by the local Harness LLMs "
        f"and **{sweep_fb_pct:.1f}%** ({sum_fb_tok / 1000:.1f}k) by the "
        f"cross-family fallback `{cfg.get('fallback_llm')}`."
    )
    lines.append("")
    lines.append(
        "- **High local share (>85%)** → the small local model carries the "
        "bulk of the eval; the fallback is doing its rescue-only job. "
        "Per-token cost is dominated by the (effectively-free) local model — "
        "fallback API spend is bounded by the failure rate.\n"
        "- **Low local share (<60%)** → the small local model is being "
        "overwhelmed (8K-context overflow on long transcripts, malformed "
        "JSON, repeated timeouts). The cost story breaks down because the "
        "fallback is doing most of the work; consider raising "
        "`--juror-call-timeout`, lowering `--context-budget`, or upgrading "
        "to a larger local model."
    )
    lines.append("")
    lines.append("## Files in this experiment folder")
    lines.append("")
    lines.append("- `config.json` — full run configuration snapshot")
    lines.append("- `summary.csv` — per-cell rows, machine-readable")
    lines.append("- `trap_coverage.md` — trap-family × Harness-LLM matrix")
    lines.append("- `cell_<harness>_x_<agent>_seedN_scoreX.X/` — one directory per cell with the harness's full JSON + Markdown report")
    lines.append("")
    lines.append("## Citation")
    lines.append("")
    lines.append(
        "```bibtex\n"
        "@misc{bousetouane2026proofagentharnessopeninfrastructure,\n"
        "      title={ProofAgent Harness: Open Infrastructure for Adversarial Evaluation of AI Agents},\n"
        "      author={Fouad Bousetouane},\n"
        "      year={2026},\n"
        "      eprint={2605.24134},\n"
        "      archivePrefix={arXiv},\n"
        "      primaryClass={cs.MA},\n"
        "      url={https://arxiv.org/abs/2605.24134},\n"
        "}\n"
        "```"
    )
    path = out_dir / "README.md"
    path.write_text("\n".join(lines))
    return path


# ═════════════════════════════════════════════════════════════════════════════
#  Utilities
# ═════════════════════════════════════════════════════════════════════════════


def _short_model_name(m: str) -> str:
    """Trim provider/org prefix and known suffixes for compact display."""
    s = m.split("/")[-1]
    s = re.sub(r"-(?:MLX|GGUF|EXL2)-?\d*(?:bit)?", "", s, flags=re.IGNORECASE)
    return s


def _safe(s: str) -> str:
    """Filename-safe."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    bundled = discover_bundled_agents()
    p = argparse.ArgumentParser(
        prog="asymmetric_evaluation_benchmarking.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Asymmetric benchmarking sweep — small local Harness LLMs × "
            "frontier agents, with cross-family fallback juror, force-"
            "fallback validation mode, compact per-turn logger, and a "
            "standalone experiment folder."
        ),
    )

    p.add_argument(
        "--agents", "-a", nargs="+", default=None, metavar="AGENT",
        help=(
            "One or more bundled agent names (or absolute spec paths). Pass "
            "'all' to use every bundled agent. Defaults to all bundled. "
            f"Available: {', '.join(bundled)}"
        ),
    )
    p.add_argument(
        "--harness-llms", nargs="+", default=DEFAULT_HARNESS_LLMS, metavar="MODEL",
        help=(
            "Small local Harness LLMs to sweep. Each must be loadable in "
            f"LM Studio (or via `lms load <name>`). Default: {len(DEFAULT_HARNESS_LLMS)} "
            "common small mlx-community models."
        ),
    )

    p.add_argument("--agent-model", default=DEFAULT_AGENT_MODEL,
                   help=f"Frontier agent LLM under test. Default: {DEFAULT_AGENT_MODEL}.")
    # --fallback-llm (v0.4.2 official name) replaces --fallback-juror (v0.4.1
    # legacy name). The fallback wraps the ENTIRE Harness pipeline (planner,
    # conductor, juror, consensus, reporter), NOT just the juror stage — the
    # old "juror" suffix was misleading. Both flag names are accepted for
    # backwards compatibility; they store into the same `fallback_llm`
    # destination on the args namespace.
    p.add_argument("--fallback-llm", "--fallback-juror",
                   dest="fallback_llm",
                   default=DEFAULT_FALLBACK_JUROR,
                   help=f"Cross-family fallback model that handles failed primary "
                        f"calls anywhere in the pipeline (planner / conductor / "
                        f"juror / consensus / reporter). Default: "
                        f"{DEFAULT_FALLBACK_JUROR}. Use 'anthropic/claude-haiku-"
                        f"4-5-20251001' for Anthropic fallback. "
                        f"(--fallback-juror is the deprecated v0.4.1 alias.)")
    p.add_argument("--proxy-url", default="http://localhost:1234/v1",
                   help="LM Studio proxy URL. Default: LM Studio at :1234.")
    p.add_argument("--no-proxy", action="store_true",
                   help="Skip proxy wiring — for testing the sweep against a cloud "
                        "Harness LLM (e.g. with --harness-llms anthropic/claude-haiku-4-5).")

    p.add_argument("--turns", "-t", type=int, default=8)
    p.add_argument("--consensus", "-c", choices=["independent", "delphi", "debate"], default="delphi")
    p.add_argument("--seed", "-s", type=int, default=42)
    p.add_argument("--context-budget", "--ctx", type=int, default=6000,
                   help="INPUT token budget for prompts the Harness builds. "
                        "6000 fits small local models well; raise to 32000+ "
                        "for long-context Harness LLMs. NOT the same as "
                        "--max-tokens (which caps OUTPUT generation).")
    p.add_argument("--max-tokens", type=int, default=8192,
                   help="Max OUTPUT (generation) tokens the Harness LLM is "
                        "allowed to write per call. Applied to BOTH the "
                        "primary AND the fallback LLM. Default 8192 fits the "
                        "~4000-token audit JSON of 50-turn debate-consensus "
                        "jurying with margin. Bump to 16384+ for turns>=100. "
                        "Lower to 2048-4096 for cost-bound smoke tests on "
                        "short evals. NOT the context window (input + output "
                        "combined — that's --context-budget).")
    p.add_argument("--per-call-timeout", type=int, default=3600,
                   help="Per-call litellm `timeout=` kwarg (seconds). Enforced "
                        "by httpx at the HTTP level. Default 3600s.")
    p.add_argument("--juror-call-timeout", type=int, default=0,
                   help="Tighten the effective per-call timeout (seconds) for "
                        "this run. 0 = use --per-call-timeout directly. If set "
                        "AND smaller than --per-call-timeout, overrides it. "
                        "When the primary times out, litellm raises an httpx "
                        "exception that our fallback wrapper catches and "
                        "routes to --fallback-llm cleanly. Recommended: 180 "
                        "(3 min) for small local models on long-context prompts "
                        "— bounds the 30-60min runaway tail latency that "
                        "overloaded 8K-context models can produce on 50-turn "
                        "debate-consensus jurying.")

    p.add_argument("--extra-traps", nargs="+", default=None, metavar="PATH",
                   help="External trap directories to merge with the bundled library.")

    p.add_argument("--force-fallback", action="store_true",
                   help="Make every primary juror call return empty, forcing the fallback "
                        "to handle each call. Validates the fallback wiring without waiting "
                        "for a real primary failure.")

    p.add_argument("--no-swap", action="store_true",
                   help="Do NOT attempt to load each Harness LLM between cells via the "
                        "`lms` CLI. Assume the current LM Studio model serves all cells.")
    p.add_argument("--interactive-swap", action="store_true",
                   help="If `lms` CLI isn't available, pause before each Harness LLM "
                        "and wait for the user to load it manually.")

    p.add_argument("--output-dir", default=None,
                   help="Output directory. Default: results/asymmetric_<timestamp>/")
    p.add_argument("--list-only", action="store_true",
                   help="Print the run matrix and exit. No API calls.")

    return p.parse_args()


def main() -> int:
    args = parse_args()

    # ── Resolve agent list ──
    agents_arg = args.agents
    if agents_arg is None or (len(agents_arg) == 1 and agents_arg[0].lower() == "all"):
        agents_arg = discover_bundled_agents()
    agent_paths = [resolve_agent_spec_path(a) for a in agents_arg]
    if not agent_paths:
        raise SystemExit("No agents resolved — pass --agents <name> or none for all.")

    # ── Plan matrix ──
    matrix = [(h, p) for h in args.harness_llms for p in agent_paths]
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (
        Path(args.output_dir) if args.output_dir
        else (DEFAULT_RESULTS_DIR / f"asymmetric_{started_at}")
    )

    # ── Header ──
    print("═" * 78, flush=True)
    print("  Asymmetric AI Agent Evaluation Benchmark", flush=True)
    print(f"  Started:        {started_at}", flush=True)
    print(f"  Agents ({len(agent_paths)}):    {', '.join(p.stem for p in agent_paths)}", flush=True)
    print(f"  Harness LLMs ({len(args.harness_llms)}): {', '.join(_short_model_name(h) for h in args.harness_llms)}", flush=True)
    print(f"  Agent model:    {args.agent_model}", flush=True)
    print(f"  Fallback LLM:   {args.fallback_llm}  (cross-family)", flush=True)
    timeout_tag = (
        f" · juror_timeout={args.juror_call_timeout}s"
        if args.juror_call_timeout else " · juror_timeout=off"
    )
    print(
        f"  Consensus:      {args.consensus} · turns={args.turns} · seed={args.seed} "
        f"· ctx={args.context_budget} (input) · max_tokens={args.max_tokens} (output)"
        f"{timeout_tag}",
        flush=True,
    )
    print(f"  Force-fallback: {'YES (validation mode)' if args.force_fallback else 'no'}", flush=True)
    print(f"  Output:         {out_dir}", flush=True)
    print(f"  Total cells:    {len(matrix)}", flush=True)
    print("═" * 78, flush=True)

    if args.list_only:
        print("\n[--list-only] Matrix:")
        for i, (h, p) in enumerate(matrix, 1):
            print(f"  {i:3d}.  {_short_model_name(h):30s} × {p.stem}")
        print(f"\nNo eval run. {len(matrix)} cells would be executed sequentially.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Load trap library once for the entire sweep ──
    traps = load_traps(extra_dirs=args.extra_traps or None)
    trap_index = TrapIndex(traps)
    composite_n = sum(1 for t in traps if "Composite attack chain" in (t.pattern or ""))
    print(f"\nTrap library: {len(traps)} traps · {len(trap_index.by_family)} families · "
          f"{composite_n} with composite attack chain in Pattern", flush=True)

    # ── Persist run config ──
    cfg = {
        "started_at": started_at,
        "agents": [p.stem for p in agent_paths],
        "harness_llms": list(args.harness_llms),
        "agent_model": args.agent_model,
        "fallback_llm": args.fallback_llm,
        "proxy_url": None if args.no_proxy else args.proxy_url,
        "consensus": args.consensus,
        "turns": args.turns,
        "seed": args.seed,
        "context_budget_tokens": args.context_budget,
        "per_call_timeout": args.per_call_timeout,
        "juror_call_timeout": args.juror_call_timeout,
        "max_tokens": args.max_tokens,
        "force_fallback": bool(args.force_fallback),
        "no_swap": bool(args.no_swap),
        "n_harness_llms": len(args.harness_llms),
        "n_agents": len(agent_paths),
        "n_cells": len(matrix),
        "trap_library_size": len(traps),
        "trap_families": list(sorted(trap_index.by_family.keys())),
        "extra_traps": list(args.extra_traps or []),
    }
    (out_dir / "config.json").write_text(json.dumps(cfg, indent=2))

    # ── Sweep ──
    cells: list[CellResult] = []
    last_harness = None
    sweep_t0 = time.time()
    for i, (h, p) in enumerate(matrix, 1):
        print(f"\n── Cell {i}/{len(matrix)} ──", flush=True)
        if h != last_harness and not args.no_proxy:
            swap_lm_studio_model(h, no_swap=args.no_swap, interactive=args.interactive_swap)
        last_harness = h
        cell = run_single_cell(
            out_dir=out_dir,
            harness_llm_name=h,
            agent_spec_path=p,
            agent_model=args.agent_model,
            fallback_llm=args.fallback_llm,
            turns=args.turns,
            consensus=args.consensus,
            seed=args.seed,
            context_budget_tokens=args.context_budget,
            per_call_timeout=args.per_call_timeout,
            juror_call_timeout=args.juror_call_timeout,
            max_tokens=args.max_tokens,
            proxy_url=None if args.no_proxy else args.proxy_url,
            extra_traps=args.extra_traps,
            force_fallback=bool(args.force_fallback),
            trap_index=trap_index,
        )
        cells.append(cell)
        # Incrementally write the summary so an early Ctrl-C still leaves
        # a valid partial CSV + cell list on disk.
        write_summary_csv(out_dir, cells)
        (out_dir / "cells.json").write_text(
            json.dumps([asdict(c) for c in cells], indent=2, default=str)
        )

    sweep_elapsed = time.time() - sweep_t0

    # ── Final reports ──
    summary_path  = write_summary_csv(out_dir, cells)
    coverage_path = write_trap_coverage(out_dir, cells, trap_index)
    readme_path   = write_overview_md(out_dir, cells, cfg)

    print("\n" + "═" * 78, flush=True)
    print("  Sweep complete", flush=True)
    print(f"    cells:         {len(cells)}", flush=True)
    print(f"    wall time:     {sweep_elapsed:.0f}s ({sweep_elapsed/60:.1f} min)", flush=True)
    print(f"    output dir:    {out_dir}", flush=True)
    print(f"    summary csv:   {summary_path.relative_to(out_dir.parent)}", flush=True)
    print(f"    trap coverage: {coverage_path.relative_to(out_dir.parent)}", flush=True)
    print(f"    readme:        {readme_path.relative_to(out_dir.parent)}", flush=True)
    print("═" * 78, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
