"""The public Harness class — the user's single entry point.

Usage (the whole API in one place):

    from proofagent_harness import Harness

    def my_agent(message: str) -> str:
        return your_llm_call(message)

    report = Harness().evaluate(
        my_agent,
        role="customer support agent",
        goal="handle refunds safely",
    )
    print(report)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from rich.console import Console

from proofagent_harness.context_budget import (
    CHARS_PER_TOKEN,
    char_budget_for,
    detect_context_tokens,
)
from proofagent_harness.graph import build_graph
from proofagent_harness.graph.state import HarnessState
from proofagent_harness.llm import LLM, default_llm
from proofagent_harness.loaders import (
    TrapIndex,
    load_knowledge,
    load_personas,
    load_skills,
    load_traps,
)
from proofagent_harness.progress import ProgressReporter
from proofagent_harness.schemas import (
    CANONICAL_METRICS,
    AgentCallable,
    AgentContext,
    Certification,
    Event,
    Report,
    Scoring,
    Severity,
    canonicalize_metric,
)


class Harness:
    """The user-facing harness. Configure once, evaluate any number of agents."""

    def __init__(
        self,
        *,
        # ── LLM ──
        llm: str | LLM | None = None,
        # ── Metrics ──
        metrics: list[str] | None = None,
        # ── Conductor ──
        turns: int = 8,
        extra_traps: list[str] | None = None,
        trap_packs: list[str] | None = None,
        # ── Jury ──
        consensus: str = "delphi",
        personas: list[str] | None = None,
        revote_threshold: float = 2.0,
        debate_rounds: int = 3,
        # ── Scoring ──
        scoring: Scoring | None = None,
        # ── Skills ──
        extra_skills: list[str] | None = None,
        # ── Output ──
        verbose: bool = True,
        seed: int | None = None,
        # ── Context-window safety net ──
        context_budget_tokens: int | None = None,
    ) -> None:
        # LLM. The user's `seed` propagates here so LiteLLM passes it through to
        # any provider that honors deterministic decoding (OpenAI, Gemini, ...).
        if isinstance(llm, LLM):
            self.llm: LLM = llm
            if seed is not None and self.llm.seed is None:
                self.llm.seed = seed
        elif isinstance(llm, str):
            self.llm = LLM(model=llm, seed=seed)
        else:
            self.llm = default_llm()
            if seed is not None:
                self.llm.seed = seed

        # Metrics — resolve aliases (e.g. "hallucination" -> "hallucination_resistance")
        raw_metrics = metrics or list(CANONICAL_METRICS)
        self.metrics = [canonicalize_metric(m) for m in raw_metrics]

        # Same alias resolution for the scoring config (critical_floors keys)
        if scoring is not None and scoring.critical_floors:
            scoring.critical_floors = {
                canonicalize_metric(k): v for k, v in scoring.critical_floors.items()
            }
        if scoring is not None and scoring.weights:
            scoring.weights = {
                canonicalize_metric(k): v for k, v in scoring.weights.items()
            }

        # Conductor
        self.turns = turns
        self.extra_traps = extra_traps or []
        self.trap_packs = trap_packs or []

        # Jury
        if consensus not in {"independent", "delphi", "debate"}:
            raise ValueError(
                f"consensus must be one of independent|delphi|debate, got: {consensus!r}"
            )
        self.consensus = consensus
        self.personas = personas or ["rigorous", "lenient", "contrarian"]
        self.revote_threshold = revote_threshold
        self.debate_rounds = debate_rounds

        # Scoring
        self.scoring = scoring or Scoring()

        # Skills
        self.extra_skills = extra_skills or []

        # Output
        self.verbose = verbose
        self.seed = seed

        # Context budget — auto-detect the model's window if the user didn't override.
        if context_budget_tokens is not None:
            self.context_budget_chars = max(1, int(context_budget_tokens)) * CHARS_PER_TOKEN
        else:
            self.context_budget_chars = char_budget_for(self.llm.model)
        self.detected_context_tokens = detect_context_tokens(self.llm.model)

        # Pre-load assets so the first eval is fast and we fail loudly on bad config.
        # The TrapIndex builds inverted lookups (by domain / metric / family / severity)
        # once here, so each subsequent eval gets O(1) selection rather than rescanning.
        self._skills = load_skills(self.extra_skills)
        self._traps = load_traps(self.extra_traps, self.trap_packs)
        self._trap_index = TrapIndex(self._traps)
        self._personas_loaded = load_personas(self.personas)

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def evaluate(
        self,
        agent: AgentCallable,
        *,
        role: str = "an AI agent",
        business_case: str = "",
        goal: str = "",
        knowledge: Any = None,
        context: AgentContext | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> Report:
        """Run a full evaluation. Synchronous wrapper around `aevaluate`.

        Works in both regular Python scripts AND inside Jupyter notebooks /
        async contexts. Jupyter's kernel runs inside an active asyncio event
        loop, so we detect that and spawn a fresh loop in a worker thread to
        avoid the `asyncio.run() cannot be called from a running event loop`
        crash. If the caller already has an event loop and wants async control,
        use `aevaluate()` instead.
        """
        kwargs = {
            "agent": agent,
            "role": role,
            "business_case": business_case,
            "goal": goal,
            "knowledge": knowledge,
            "context": context,
            "on_event": on_event,
        }

        # Are we already inside a running event loop? (Jupyter == yes.)
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if not in_loop:
            # Standard CLI / script path — no active loop, just run the coro.
            return asyncio.run(self.aevaluate(**kwargs))

        # We're inside a running loop. Spin the coroutine on a fresh loop in
        # a worker thread; this keeps Jupyter's loop free and avoids the
        # nested-asyncio.run() error.
        import threading

        box: dict[str, Any] = {}

        def _thread_runner() -> None:
            try:
                box["result"] = asyncio.run(self.aevaluate(**kwargs))
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                box["exc"] = exc

        t = threading.Thread(target=_thread_runner, daemon=True)
        t.start()
        t.join()

        if "exc" in box:
            raise box["exc"]
        return box["result"]

    async def aevaluate(
        self,
        agent: AgentCallable,
        *,
        role: str = "an AI agent",
        business_case: str = "",
        goal: str = "",
        knowledge: Any = None,
        context: AgentContext | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> Report:
        """Run a full evaluation asynchronously."""
        start = time.time()

        # Wire up progress UI + user event hook
        progress = ProgressReporter(enabled=self.verbose)
        composed_callback = _compose_callbacks(progress.on_event, on_event)

        if self.verbose:
            progress.start(turn_count=self.turns)

        try:
            # ── Phase 0: pre-flight check ────────────────────────────────
            # Verify the Harness LLM is reachable + the API key is valid
            # BEFORE running any planner / conductor / juror logic. Fails
            # fast with an actionable error instead of producing a misleading
            # all-fallback scorecard 30 seconds later.
            composed_callback(
                Event(type="setup_start", detail="checking Harness LLM reachability")
            )
            await self._preflight_check_llm()
            composed_callback(
                Event(
                    type="setup_done",
                    detail=f"Harness LLM reachable ({self.llm.model})",
                )
            )

            initial_state = self._build_initial_state(
                agent=agent,
                role=role,
                business_case=business_case,
                goal=goal,
                knowledge=knowledge,
                context=context,
                on_event=composed_callback,
            )

            graph = build_graph()
            final_state = await graph.ainvoke(initial_state)

            report = self._state_to_report(final_state, duration=time.time() - start)
            composed_callback(Event(type="done"))
            return report
        finally:
            if self.verbose:
                progress.stop()
                # Print the final scorecard
                Console().print(report if "report" in locals() else "")  # type: ignore[possibly-undefined]

    # ──────────────────────────────────────────────────────────────────
    # Private
    # ──────────────────────────────────────────────────────────────────

    def _build_initial_state(
        self,
        *,
        agent: AgentCallable,
        role: str,
        business_case: str,
        goal: str,
        knowledge: Any,
        context: AgentContext | None,
        on_event: Callable[[Event], None],
    ) -> HarnessState:
        ctx = context or AgentContext()
        # If knowledge is passed top-level, surface it both ways:
        # - in `context.knowledge` for downstream code that reads context
        # - in `knowledge_text` (loaded once) for jurors that need ground truth
        knowledge_source = knowledge if knowledge is not None else ctx.knowledge
        knowledge_text = load_knowledge(knowledge_source) if knowledge_source else ""

        state: HarnessState = {
            "role": role,
            "business_case": business_case,
            "goal": goal,
            "turn_count": int(self.turns),
            "metrics": list(self.metrics),
            "knowledge_text": knowledge_text,
            "context": ctx,
            "agent_callable": agent,
            "skills": self._skills,
            "traps": self._traps,
            "trap_index": self._trap_index,
            "personas": self._personas_loaded,
            "consensus_strategy": self.consensus,
            "debate_rounds": int(self.debate_rounds),
            "revote_threshold": float(self.revote_threshold),
            "scoring_config": self.scoring,
            "on_event": on_event,
            "current_turn": 0,
            "transcript": [],
            "round_one_scores": [],
            "round_two_scores": [],
            "metrics_to_revote": [],
            "consensus": {},
            "cost_usd": 0.0,
            "tokens_used": 0,
            "llm": self.llm,
            "context_budget_chars": self.context_budget_chars,
        }
        return state

    def _state_to_report(self, state: dict[str, Any], duration: float) -> Report:
        consensus = state.get("consensus", {})
        severity = {m: consensus[m].severity for m in consensus}

        try:
            cert = Certification(state.get("certification") or "NOT_READY")
        except ValueError:
            cert = Certification.NOT_READY

        return Report(
            final_score=float(state.get("final_score") or 0.0),
            certification=cert,
            per_metric=dict(state.get("per_metric") or {}),
            confidence=dict(state.get("confidence") or {}),
            severity={m: (severity.get(m) or Severity.WARN) for m in (state.get("per_metric") or {})},
            transcript=list(state.get("transcript") or []),
            consensus_log=consensus,
            findings=list(state.get("findings") or []),
            summary=str(state.get("summary") or ""),
            cost_usd=float(self.llm.total_cost_usd),
            duration_seconds=round(duration, 2),
            tokens_used=int(self.llm.total_tokens),
            metadata={
                "model": self.llm.model,
                "consensus_strategy": self.consensus,
                "personas": self.personas,
                "metrics": self.metrics,
                "turns": self.turns,
                "llm_call_count": self.llm.call_count,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


    async def _preflight_check_llm(self) -> None:
        """Confirm the Harness LLM is reachable and the API key is valid.

        Runs ONCE at the start of every evaluation, before any planning,
        conducting, or scoring work. Costs ~5 tokens; saves ~30 seconds of
        wasted work + a misleading scorecard when auth fails.
        """
        try:
            await self.llm.complete(
                [{"role": "user", "content": "ok"}],
                max_tokens=5,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised wrapped below
            # Determine the right env var name for the diagnostic hint
            model = self.llm.model.lower()
            if "claude" in model or "anthropic" in model:
                env_hint = "ANTHROPIC_API_KEY"
            elif "gpt" in model or "openai" in model:
                env_hint = "OPENAI_API_KEY"
            elif "gemini" in model:
                env_hint = "GEMINI_API_KEY"
            elif "bedrock" in model:
                env_hint = "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY"
            else:
                env_hint = "the provider-specific env var"

            raise LLMNotConfiguredError(
                "Harness LLM pre-flight check failed — the harness cannot "
                "reach the model and refused to start the evaluation.\n\n"
                f"  Model:       {self.llm.model}\n"
                f"  Error:       {type(exc).__name__}: {exc}\n"
                f"  Expected env var: {env_hint}\n\n"
                "Common fixes:\n"
                "  - Verify your API key is loaded INSIDE the kernel:\n"
                f"      import os; print(os.environ.get({env_hint!r}))\n"
                "  - Make sure you exported the key in the SAME terminal that\n"
                "    launched Jupyter (env vars don't carry across terminals).\n"
                "  - Confirm the model id passed to Harness(llm=...) is valid\n"
                "    and your account has access to it.\n"
                "  - For mixed-provider setups, set every relevant key:\n"
                "      ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY"
            ) from exc


class LLMNotConfiguredError(RuntimeError):
    """Raised when the Harness LLM cannot authenticate or reach the provider."""


def _compose_callbacks(
    *callbacks: Callable[[Event], None] | None,
) -> Callable[[Event], None]:
    real = [cb for cb in callbacks if cb is not None]

    def _fan(event: Event) -> None:
        for cb in real:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                pass

    return _fan
