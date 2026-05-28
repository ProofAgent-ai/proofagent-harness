"""The public Harness class — the user's single entry point."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

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
        llm: str | LLM | None = None,
        fallback_llm: str | LLM | None = None,
        max_tokens: int | None = None,
        metrics: list[str] | None = None,
        turns: int = 8,
        extra_traps: list[str] | None = None,
        trap_packs: list[str] | None = None,
        consensus: str = "delphi",
        personas: list[str] | None = None,
        revote_threshold: float = 1.0,
        debate_rounds: int = 3,
        scoring: Scoring | None = None,
        extra_skills: list[str] | None = None,
        verbose: bool = True,
        seed: int | None = None,
        context_budget_tokens: int | None = None,
    ) -> None:
        """Configure a Harness.

        Parameters
        ----------
        llm:
            Primary Harness LLM used for ALL internal scoring (planner,
            conductor, juror, consensus, reporter). String like
            ``"claude-sonnet-4-6"`` is auto-wrapped as ``LLM(model=...)``.
            Pass an ``LLM`` instance directly for advanced configuration.
        fallback_llm:
            **(Optional, v0.4.2)** A secondary LLM that automatically
            handles primary failures. Most useful when ``llm=`` is a small
            local model (Gemma 4B, Llama-3.2-3B, etc.) that may produce
            malformed JSON or time out on long-context juror prompts.
            When configured, failed primary calls route to the fallback
            with the **original prompt** (no error-message append) — see
            :class:`LLM` docstring for the asymmetric-cost design rationale.
            Default ``None`` = no fallback; failures surface as actionable
            :class:`LLMJSONStructureError` exceptions instead.

            Example::

                from proofagent_harness import Harness
                report = Harness(
                    llm="openai/gemma-4-E4B-it-MLX-8bit",
                    fallback_llm="anthropic/claude-haiku-4-5-20251001",
                    turns=50, consensus="debate",
                ).evaluate(agent, ...)

                # Inspect the asymmetric cost split:
                print(report.token_split)  # {'primary': 0.91, 'fallback': 0.09}
        max_tokens:
            **(Optional, v0.4.3)** Max OUTPUT (generation) tokens the
            Harness LLM is allowed to write per call. This is NOT the
            context window (input+output budget, much larger — 200K-1M
            for frontier models). It is purely the cap on the model's
            REPLY length.

            Default ``None`` = use ``LLM.max_tokens`` default (``8192``).
            Bump higher (``16384``, ``32768``) for very long evals
            (turns≥100). Lower for cost-bound smoke tests on cheap models.

            When ``llm=`` is a STRING, this is passed to the LLM Harness
            constructs. When ``llm=`` is a pre-built ``LLM`` instance, this
            kwarg is IGNORED — your instance configuration wins.

            Same value also applied to the constructed ``fallback_llm``.

            Why this matters: at ``turns=50`` with debate consensus, each
            juror call needs to write ~4000 output tokens of audit JSON
            (50 per-turn entries + reasoning). The pre-v0.4.3 default of
            ``2048`` truncated these responses mid-string, producing
            unparseable JSON and (correctly) triggering the fallback —
            but the fallback also hit the same 2048 cap, so both failed.
            ``8192`` fits 50-turn audits comfortably.
        """
        # ── max_tokens (v0.4.3): the max OUTPUT (generation) cap propagated
        # to any LLM the Harness constructs from a string. Defaults to
        # LLM's class default (8192). If the user passes a pre-built LLM
        # instance, we respect their configuration — we never silently
        # override their max_tokens. The instance form is the escape hatch
        # for fine-grained per-LLM control.
        _llm_kwargs: dict[str, Any] = {"seed": seed}
        if max_tokens is not None:
            _llm_kwargs["max_tokens"] = max_tokens

        if isinstance(llm, LLM):
            self.llm: LLM = llm
            if seed is not None and self.llm.seed is None:
                self.llm.seed = seed
        elif isinstance(llm, str):
            self.llm = LLM(model=llm, **_llm_kwargs)
        else:
            self.llm = default_llm()
            if seed is not None:
                self.llm.seed = seed
            if max_tokens is not None:
                self.llm.max_tokens = max_tokens

        # ── Resolve fallback_llm and attach to primary so EVERY internal
        # call (planner, conductor, juror, consensus, reporter) gets
        # transparent rescue. The primary handles the bulk of the work;
        # the fallback only fires on JSON failure, empty content, or
        # exception. The fallback gets the ORIGINAL prompt (the v0.4.2
        # bug fix — see LLM.complete_json docstring).
        self.fallback_llm: LLM | None = None
        if fallback_llm is not None:
            if isinstance(fallback_llm, LLM):
                self.fallback_llm = fallback_llm
            elif isinstance(fallback_llm, str):
                self.fallback_llm = LLM(model=fallback_llm, **_llm_kwargs)
            else:
                raise TypeError(
                    f"fallback_llm must be a str or LLM instance, got "
                    f"{type(fallback_llm).__name__!s}"
                )
            self.llm.fallback_llm = self.fallback_llm

        raw_metrics = metrics or list(CANONICAL_METRICS)
        self.metrics = [canonicalize_metric(m) for m in raw_metrics]

        if scoring is not None and scoring.critical_floors:
            scoring.critical_floors = {
                canonicalize_metric(k): v for k, v in scoring.critical_floors.items()
            }
        if scoring is not None and scoring.weights:
            scoring.weights = {
                canonicalize_metric(k): v for k, v in scoring.weights.items()
            }

        self.turns = turns
        self.extra_traps = extra_traps or []
        self.trap_packs = trap_packs or []

        if consensus not in {"independent", "delphi", "debate"}:
            raise ValueError(
                f"consensus must be one of independent|delphi|debate, got: {consensus!r}"
            )
        self.consensus = consensus
        self.personas = personas or ["rigorous", "lenient", "contrarian"]
        self.revote_threshold = revote_threshold
        self.debate_rounds = debate_rounds

        self.scoring = scoring or Scoring()

        self.extra_skills = extra_skills or []

        self.verbose = verbose
        self.seed = seed

        if context_budget_tokens is not None:
            self.context_budget_chars = max(1, int(context_budget_tokens)) * CHARS_PER_TOKEN
        else:
            self.context_budget_chars = char_budget_for(self.llm.model)
        self.detected_context_tokens = detect_context_tokens(self.llm.model)

        self._skills = load_skills(self.extra_skills)
        self._traps = load_traps(self.extra_traps, self.trap_packs)
        self._trap_index = TrapIndex(self._traps)
        self._personas_loaded = load_personas(self.personas)

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
        """Run a full evaluation. Synchronous wrapper around `aevaluate`."""
        kwargs = {
            "agent": agent,
            "role": role,
            "business_case": business_case,
            "goal": goal,
            "knowledge": knowledge,
            "context": context,
            "on_event": on_event,
        }

        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if not in_loop:
            return asyncio.run(self.aevaluate(**kwargs))

        import threading

        box: dict[str, Any] = {}

        def _thread_runner() -> None:
            try:
                box["result"] = asyncio.run(self.aevaluate(**kwargs))
            except BaseException as exc:
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

        progress = ProgressReporter(enabled=self.verbose)
        composed_callback = _compose_callbacks(progress.on_event, on_event)

        # Wire LLM.on_fallback → harness Event stream. Subscribers to
        # `on_event` will receive a `fallback_triggered` Event each time
        # the fallback LLM is invoked (with payload: primary_model,
        # fallback_model, stage, reason). The LLM also prints a short
        # progress line to stdout regardless of subscribers.
        if self.fallback_llm is not None:
            def _on_fallback(payload: dict[str, Any]) -> None:
                composed_callback(Event(
                    type="fallback_triggered",
                    detail=(
                        f"{payload.get('primary_model', '?')} → "
                        f"{payload.get('fallback_model', '?')} "
                        f"({payload.get('stage', '?')}: {payload.get('reason', '?')})"
                    ),
                    payload=payload,
                ))
            self.llm.on_fallback = _on_fallback

        if self.verbose:
            progress.start(turn_count=self.turns)

        try:
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
                Console().print(report if "report" in locals() else "")

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
            "seed": self.seed,
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

        # v0.4.2: per-source accounting for the asymmetric-cost story.
        primary_tot = self.llm.primary_prompt_tokens + self.llm.primary_completion_tokens
        fb_tot = self.llm.fallback_prompt_tokens + self.llm.fallback_completion_tokens
        grand_tot = primary_tot + fb_tot
        fb_rate = (
            self.llm.fallback_call_count
            / (self.llm.primary_call_count + self.llm.fallback_call_count)
            if (self.llm.primary_call_count + self.llm.fallback_call_count) > 0
            else 0.0
        )
        token_split: dict[str, float] = {}
        if self.fallback_llm is not None and grand_tot > 0:
            token_split = {
                "primary": round(primary_tot / grand_tot, 4),
                "fallback": round(fb_tot / grand_tot, 4),
            }

        return Report(
            final_score=float(state.get("final_score") or 0.0),
            certification=cert,
            per_metric=dict(state.get("per_metric") or {}),
            confidence=dict(state.get("confidence") or {}),
            severity={m: (severity.get(m) or Severity.WARN) for m in (state.get("per_metric") or {})},
            transcript=list(state.get("transcript") or []),
            consensus_log=consensus,
            findings=list(state.get("findings") or []),
            warnings=list(state.get("warnings") or []),
            summary=str(state.get("summary") or ""),
            duration_seconds=round(duration, 2),
            tokens_used=int(self.llm.total_tokens),
            # ── v0.4.2 per-source LLM accounting ──
            primary_llm_model=self.llm.model,
            primary_call_count=int(self.llm.primary_call_count),
            primary_prompt_tokens=int(self.llm.primary_prompt_tokens),
            primary_completion_tokens=int(self.llm.primary_completion_tokens),
            primary_cost_usd=float(self.llm.primary_cost_usd),
            fallback_llm_model=(self.fallback_llm.model if self.fallback_llm else ""),
            fallback_call_count=int(self.llm.fallback_call_count),
            fallback_prompt_tokens=int(self.llm.fallback_prompt_tokens),
            fallback_completion_tokens=int(self.llm.fallback_completion_tokens),
            fallback_cost_usd=float(self.llm.fallback_cost_usd),
            fallback_rate=round(fb_rate, 4),
            token_split=token_split,
            metadata={
                "model": self.llm.model,
                "fallback_model": self.fallback_llm.model if self.fallback_llm else None,
                "consensus_strategy": self.consensus,
                "personas": self.personas,
                "metrics": self.metrics,
                "turns": self.turns,
                "llm_call_count": self.llm.call_count,
            },
        )

    def _estimate_required_tokens(self) -> int:
        """Conservative estimate of the worst-case juror prompt size in tokens."""
        fixed_overhead = 4000
        per_turn = 500
        response_reserve = 2048
        safety = 512
        return fixed_overhead + self.turns * per_turn + response_reserve + safety

    async def _preflight_check_llm(self) -> None:
        """Confirm the Harness LLM is reachable, the API key is valid, AND"""
        try:
            await self.llm.complete(
                [{"role": "user", "content": "ok"}],
                max_tokens=5,
                temperature=0,
            )
        except Exception as exc:
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

        needed = self._estimate_required_tokens()
        advertised = detect_context_tokens(self.llm.model)

        if advertised >= needed * 2:
            return

        probe_input_tokens = max(2000, needed - 2048 - 512)
        probe_text = "x " * (probe_input_tokens * CHARS_PER_TOKEN // 2)
        try:
            await self.llm.complete(
                [{"role": "user", "content": probe_text}],
                max_tokens=10,
                temperature=0,
            )
        except Exception as exc:
            msg = str(exc).lower()
            ctx_signals = (
                "context length",
                "context window",
                "tokens to keep",
                "too many tokens",
                "exceeds",
                "maximum context",
                "max_tokens",
                "prompt is too long",
            )
            if not any(sig in msg for sig in ctx_signals):
                raise

            raise LLMNotConfiguredError(
                "Harness LLM pre-flight check failed — the configured harness "
                "LLM cannot handle the context size this eval will require, "
                "so the harness refuses to start.\n\n"
                f"  Model:                {self.llm.model}\n"
                f"  Advertised window:    {advertised:,} tokens\n"
                f"  Estimated need:       {needed:,} tokens "
                f"(turns={self.turns}, metrics={len(self.metrics)})\n"
                f"  Probe error:          {type(exc).__name__}: {exc}\n\n"
                "Likely cause: the proxy serving this model was loaded with "
                "a smaller context window than the model itself supports. "
                "LM Studio / Ollama / vLLM all default to small n_ctx unless "
                "you set it explicitly.\n\n"
                "Three fixes (in order of preference):\n"
                "  1. Reload your model with a larger context window:\n"
                "       LM Studio  → Load Settings → Context Length → 32768\n"
                "       Ollama     → OLLAMA_NUM_CTX=32768  ollama run ...\n"
                "       vLLM       → --max-model-len 32768\n"
                "  2. Lower the turn budget so the transcript stays small:\n"
                f"       Harness(turns={max(4, self.turns // 4)}, ...)\n"
                "  3. Use a more capable harness LLM (the scoring/conducting\n"
                "     LLM, not the agent under test). 8-13B models are too\n"
                "     small to act as the harness LLM on real-length\n"
                "     transcripts:\n"
                '       Harness(llm="gpt-4.1", ...)\n'
                '       Harness(llm="anthropic/claude-sonnet-4-6", ...)\n\n'
                "Aborting before evaluation starts. No real tokens spent."
            ) from exc

class LLMNotConfiguredError(RuntimeError):
    """Raised when the Harness LLM cannot authenticate, reach the provider,"""

def _compose_callbacks(
    *callbacks: Callable[[Event], None] | None,
) -> Callable[[Event], None]:
    real = [cb for cb in callbacks if cb is not None]

    def _fan(event: Event) -> None:
        for cb in real:
            with contextlib.suppress(Exception):
                cb(event)

    return _fan
