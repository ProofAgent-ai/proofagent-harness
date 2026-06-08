"""The public Harness class — the user's single entry point."""

from __future__ import annotations

import asyncio
import contextlib
import os
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


def _is_openai_like_model(name: str) -> bool:
    """Heuristic: does this model string route through the OpenAI API?

    Used to decide whether to pin api_base for the constructed fallback LLM
    (v0.4.4 fix — see Harness.__init__ docstring). True for explicit
    'openai/*' prefix AND for bare model names that everyone recognizes as
    OpenAI products (gpt-*, o1-*, o3-*, o4-*). False for everything else,
    including Anthropic / Gemini / Mistral / local proxies.

    Conservative on purpose: false positives (treating a non-OpenAI model
    as OpenAI) would pin api_base wrongly and break the call. Better to
    miss and let the user pass a pre-built LLM with explicit api_base.
    """
    n = name.lower().strip()
    if n.startswith("openai/"):
        return True
    # Bare model names commonly used as OpenAI shortcuts in litellm.
    bare_prefixes = ("gpt-", "o1-", "o3-", "o4-", "chatgpt-", "text-davinci")
    return any(n.startswith(p) for p in bare_prefixes)


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
        live_reporting: bool = False,
        live_reporting_config: "ReportingConfig | None" = None,
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
                # User passed a pre-built LLM — respect their configuration
                # verbatim (api_base, max_tokens, etc.). This is the escape
                # hatch for custom OpenAI-compatible endpoints (Azure, vLLM).
                self.fallback_llm = fallback_llm
            elif isinstance(fallback_llm, str):
                # v0.4.4: when the fallback is an OpenAI-flavored string AND
                # we're constructing the LLM ourselves, pin api_base to the
                # canonical OpenAI endpoint so the fallback bypasses any
                # OPENAI_BASE_URL env var the user may have set for the
                # primary (e.g. proxy_url → LM Studio in the asymmetric
                # benchmark). Without this, OpenAI fallback calls misroute
                # to the local proxy and fail with "No models loaded".
                fb_kwargs = dict(_llm_kwargs)
                if _is_openai_like_model(fallback_llm):
                    fb_kwargs.setdefault(
                        "api_base", "https://api.openai.com/v1"
                    )
                self.fallback_llm = LLM(model=fallback_llm, **fb_kwargs)
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

        # Live Reporting (opt in, off by default). When enabled but
        # PROOFAGENT_API_KEY is unset, the reporter is a no op + prints
        # a single warning. Network failures NEVER fail the evaluation.
        self._reporter = None
        if live_reporting or live_reporting_config is not None:
            try:
                from proofagent_harness.reporting import LiveReporter, ReportingConfig
                cfg = live_reporting_config or ReportingConfig()
                self._reporter = LiveReporter(cfg)
            except Exception:
                # Reporting module is intentionally permissive — if it
                # can't even import (e.g. httpx missing), reporting is
                # silently disabled. The evaluation always proceeds.
                self._reporter = None

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

            # v0.4.4 defense-in-depth: also preflight the fallback LLM and
            # verify the responding model identity matches what we asked for.
            # Catches misrouting where a proxy (LM Studio, Azure custom
            # endpoint, etc.) silently answered with a different model. If
            # the fallback can't be reached OR responds from the wrong
            # model, we abort BEFORE the eval starts — saves hours of wall
            # time + API spend on a run that would have collapsed mid-cell.
            if self.fallback_llm is not None:
                composed_callback(Event(
                    type="setup_start",
                    detail=f"verifying fallback LLM ({self.fallback_llm.model})",
                ))
                await self._preflight_check_fallback_llm()
                composed_callback(Event(
                    type="setup_done",
                    detail=(
                        f"Fallback LLM reachable ({self.fallback_llm.model}) "
                        f"via api_base={self.fallback_llm.api_base or '<provider default>'}"
                    ),
                ))

            # Live Reporting: announce the run BEFORE the graph runs so the
            # dashboard URL prints up-front. Users can click it and watch the
            # turn-by-turn progress stream in live. Best-effort — if the
            # backend is unreachable, we set _announced_run_id=None and the
            # report_completion path falls back to POST /runs/sync at the end.
            self._announced_run_id = None
            if self._reporter is not None:
                try:
                    # Resolve a meaningful agent_name (separate from role).
                    # Priority: explicit env var, callable __name__, fallback.
                    _agent_name = (
                        os.environ.get("PROOFAGENT_AGENT_NAME")
                        or getattr(agent, "__name__", None)
                        or "agent"
                    )
                    if _agent_name == "<lambda>" or _agent_name == "agent":
                        _agent_name = (role or "agent")[:64]

                    _agent_model_announce = (
                        os.environ.get("PROOFAGENT_AGENT_MODEL")
                        or "unknown"
                    )

                    resp = self._reporter.announce_run_start(
                        cell_label=f"{self.llm.model} x {role}",
                        agent_name=_agent_name,
                        agent_model=_agent_model_announce,
                        harness_llm=self.llm.model,
                        seed=self.seed,
                        turns_total=self.turns,
                        # Send EVERY piece of context the dashboard's
                        # Run-context panel renders DURING the eval
                        # (before /sync fires with the full report).
                        # Backend stores these in runs.agent_config JSONB,
                        # GET /runs/{id} surfaces them at top level.
                        config={
                            "agent_name": _agent_name,
                            "role": (role or "")[:200],
                            "business_case": (business_case or "")[:200],
                            "goal": (goal or "")[:200],
                            "harness_llm": self.llm.model,
                            "agent_model": _agent_model_announce,
                            "consensus_strategy": getattr(self, "consensus", "delphi"),
                            "metrics_active": list(getattr(self, "metrics", []) or []),
                            "seed": int(self.seed) if self.seed is not None else None,
                        },
                    )
                    if resp:
                        self._announced_run_id = resp.get("run_id")
                        url = resp.get("dashboard_url")
                        if url and self._reporter.cfg.print_progress:
                            # Boxed, hard-to-miss banner so the user can click
                            # the URL and watch the eval land live.
                            bar = "═" * 64
                            self._reporter._print("")
                            self._reporter._print(f"╔{bar}╗")
                            self._reporter._print(f"║  Live Reporting — your dashboard URL                           ║")
                            self._reporter._print(f"╠{bar}╣")
                            self._reporter._print(f"║  {url:<62}║")
                            self._reporter._print(f"╚{bar}╝")
                            self._reporter._print(
                                "  Open the link above to watch the evaluation stream in."
                            )
                            self._reporter._print("")
                except Exception:
                    # Never block the eval on a reporting hiccup.
                    self._announced_run_id = None

            # Live Reporting: stream per-turn updates to the dashboard so
            # the progress bar climbs + transcript fills in turn-by-turn.
            # We wrap composed_callback HERE (after announce) so we only
            # add the network call when we have a run_id and a reporter.
            # Best-effort, fully isolated — never blocks/raises into the
            # conductor loop.
            graph_callback = composed_callback
            announced_run_id = getattr(self, "_announced_run_id", None)
            if announced_run_id and self._reporter is not None:
                _reporter = self._reporter
                _local_callback = composed_callback

                # Collect every Event into a local list so /sync can backfill
                # the run_events table even if individual /events POSTs were
                # lost to network failure. Makes /sync the source of truth.
                _collected_events: list[Event] = []
                self._collected_events = _collected_events  # for report_completion

                def _live_reporting_callback(ev: Event) -> None:
                    # Always retain locally — /sync uses this list to
                    # backfill the run_events table as a guaranteed
                    # backstop in case live POSTs failed.
                    try:
                        _collected_events.append(ev)
                    except Exception:
                        pass
                    # Fire the local callback first so progress UI + user
                    # subscribers stay sharp even if the network hiccups.
                    try:
                        _local_callback(ev)
                    except Exception:
                        pass
                    # Stream the full Event to the dashboard activity feed.
                    # The dashboard renders a terminal-style chronological
                    # log + token-spend / fallback / LLM-call insights from
                    # these. Best-effort, 3 s timeout, never blocks.
                    try:
                        _reporter.append_event(
                            run_id=str(announced_run_id),
                            event_type=ev.type,
                            detail=ev.detail,
                            payload=ev.payload or {},
                            turn=ev.turn,
                        )
                    except Exception:
                        pass
                    # Per-turn SYNCHRONOUS commit — guarantees the
                    # dashboard's transcript + progress bar reflects
                    # turn N before the harness starts turn N+1. Also
                    # prints a hard-to-miss progress bar in the terminal.
                    # The reporter handles its own retries (3 attempts,
                    # exponential backoff). Events from this turn are
                    # flushed first so chronological order is preserved
                    # on the activity feed.
                    if ev.type == "turn_end" and ev.turn is not None:
                        try:
                            _reporter.append_turn(
                                run_id=str(announced_run_id),
                                turn_index=int(ev.turn),
                                question=str(ev.payload.get("question", "")),
                                answer=str(ev.payload.get("answer", "")),
                                trap_name=ev.payload.get("trap_name"),
                                defects=list(ev.payload.get("defects", []) or []),
                                duration_s=float(ev.payload.get("duration_s", 0.0) or 0.0),
                                outcome=str(ev.payload.get("outcome") or "ok"),
                                # NEW: pass total_turns so the reporter
                                # can render a per-turn progress bar like
                                # [report] ████████░░ 80% turn 4/5 → synced
                                total_turns=int(self.turns),
                            )
                            # Track per-turn sync outcome on the reporter
                            # so summary banner shows the real number sent
                            # (was using bg worker counters which now
                            # aren't applicable for turn_events anymore).
                            _reporter._turn_events_sent_sync = getattr(
                                _reporter, "_turn_events_sent_sync", 0,
                            ) + 1
                        except Exception:
                            pass

                graph_callback = _live_reporting_callback

            initial_state = self._build_initial_state(
                agent=agent,
                role=role,
                business_case=business_case,
                goal=goal,
                knowledge=knowledge,
                context=context,
                on_event=graph_callback,
            )

            graph = build_graph()
            final_state = await graph.ainvoke(initial_state)

            report = self._state_to_report(final_state, duration=time.time() - start)
            composed_callback(Event(type="done"))

            # Live Reporting: side car, always best effort, never raises.
            # Skipped silently if reporter is None (live_reporting=False).
            if self._reporter is not None:
                try:
                    # Collect every trap the planner used so the dashboard
                    # can show "Tested against these adversarial scenarios"
                    # — critical for subscription-grade reports.
                    traps_used_list: list[dict[str, Any]] = []
                    try:
                        plan = (final_state or {}).get("plan") if isinstance(final_state, dict) else None
                        if plan and hasattr(plan, "turns"):
                            seen: set[str] = set()
                            for ts in plan.turns:
                                tr = getattr(ts, "trap", None)
                                if tr and getattr(tr, "name", "") not in seen:
                                    seen.add(tr.name)
                                    traps_used_list.append({
                                        "name": getattr(tr, "name", ""),
                                        "family": getattr(tr, "family", ""),
                                        "severity": getattr(tr, "severity", "medium"),
                                        "metrics": list(getattr(tr, "metrics", []) or []),
                                        "pass_criteria": (getattr(tr, "pass_criteria", "") or "")[:300],
                                        "tags": list(getattr(tr, "tags", []) or []),
                                    })
                    except Exception:
                        pass

                    # Pull the agent's actual model name if the user wired
                    # one (env, callable __name__, etc.) — falls back to
                    # whatever the conductor recorded on report.
                    agent_model_str = (
                        os.environ.get("PROOFAGENT_AGENT_MODEL")
                        or getattr(report, "agent_model", "")
                        or getattr(agent, "__name__", "")
                        or "unknown"
                    )

                    self._reporter.report_completion(
                        run_id=getattr(self, "_announced_run_id", None),
                        cell_label=f"{self.llm.model} x {role}",
                        report_blob=_report_to_sync_payload(
                            report, role=role, seed=self.seed,
                            harness_llm=self.llm.model,
                            business_case=business_case,
                            goal=goal,
                            agent_model=agent_model_str,
                            consensus_strategy=getattr(self, "consensus", "delphi"),
                            metrics_active=list(getattr(self, "metrics", []) or []),
                            traps_used=traps_used_list,
                        ),
                        transcript=_transcript_to_payload(report),
                        findings=_findings_to_payload(report),
                        # Full event log — backend backfills run_events
                        # table from this if live POSTs were lost. Makes
                        # /sync the atomic source of truth.
                        events=[
                            {
                                "event_type": getattr(ev, "type", "unknown"),
                                "detail": getattr(ev, "detail", "") or "",
                                "payload": dict(getattr(ev, "payload", {}) or {}),
                                "turn": getattr(ev, "turn", None),
                            }
                            for ev in getattr(self, "_collected_events", [])
                        ],
                    )
                except Exception:
                    # Reporting failure NEVER fails an evaluation
                    pass

            return report
        finally:
            if self.verbose:
                progress.stop()
                Console().print(report if "report" in locals() else "")
            # ALWAYS print the Live Reporting summary banner at the very
            # end (even on crash / Ctrl+C) so the user sees exactly which
            # POST succeeded vs failed. Without this, fire-and-forget
            # POST failures (per-turn, per-event) were invisible and the
            # dashboard would stay empty with no terminal hint why.
            if self._reporter is not None:
                try:
                    self._reporter.print_summary_banner()
                except Exception:
                    pass
                # Production-grade lifecycle: flush + shut down the
                # background reporting thread. Critical for pytest /
                # CI users — without this the daemon thread sits idle
                # after the eval and may not flush its queue if the
                # process exits abruptly. close() is idempotent.
                try:
                    self._reporter.close()
                except Exception:
                    pass

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

    async def _preflight_check_fallback_llm(self) -> None:
        """v0.4.4: confirm the fallback LLM is reachable AND that the
        responding model identity matches what we asked for.

        Catches two classes of misrouting that v0.4.1-4.3 hit at runtime:

          1. Network unreachable / wrong API key → fail-fast with a clear
             error before the eval burns hours of wall time and API spend.
          2. Proxy intercepted the call and returned a response from a
             DIFFERENT model (e.g. ``OPENAI_BASE_URL`` pointed at LM Studio,
             which has Gemma loaded but responded to a request for
             ``gpt-4.1-mini``) → fail-fast with the actual returned model
             name in the error message.

        The v0.4.4 ``api_base`` auto-pin (in ``Harness.__init__``) is the
        primary defense; this preflight is defense-in-depth so a future
        regression OR a user's custom routing CAN'T silently misroute the
        fallback and waste a multi-hour run.
        """
        if self.fallback_llm is None:
            return

        try:
            r = await self.fallback_llm.complete(
                [{"role": "user", "content": "ok"}],
                max_tokens=5,
                temperature=0,
            )
        except Exception as exc:
            raise LLMNotConfiguredError(
                "Fallback LLM pre-flight check failed — the harness cannot "
                "reach the configured fallback and refused to start the "
                "evaluation.\n\n"
                f"  Fallback model:    {self.fallback_llm.model}\n"
                f"  Fallback api_base: {self.fallback_llm.api_base or '<provider default>'}\n"
                f"  Error:             {type(exc).__name__}: {exc}\n\n"
                "Likely causes:\n"
                "  1. Missing API key — check that the provider-matching env\n"
                "     var is set (ANTHROPIC_API_KEY / OPENAI_API_KEY /\n"
                "     GEMINI_API_KEY) IN THE SAME shell that launched the run.\n"
                "  2. Network issue — verify api.openai.com / api.anthropic.com\n"
                "     is reachable from this machine.\n"
                "  3. Wrong endpoint — if you have OPENAI_BASE_URL set for a\n"
                "     local proxy AND your fallback is openai/*, v0.4.4 pins\n"
                "     api_base=https://api.openai.com/v1 automatically. For\n"
                "     custom endpoints (Azure, vLLM), pass the fallback as a\n"
                "     pre-built LLM instance with explicit api_base=.\n\n"
                "Aborting before evaluation starts. No real tokens spent on "
                "the eval (one tiny preflight call may have been billed)."
            ) from exc

        # Model-identity check — confirm a proxy didn't silently respond
        # with a different model than requested.
        returned_model = ""
        try:
            returned_model = (r.raw or {}).get("model") or ""
        except Exception:
            returned_model = ""

        if returned_model and not _models_match(
            self.fallback_llm.model, returned_model
        ):
            raise LLMNotConfiguredError(
                "Fallback LLM responded — but from a DIFFERENT model than "
                "configured. Aborting to prevent silently scoring with the "
                "wrong fallback.\n\n"
                f"  Requested fallback: {self.fallback_llm.model}\n"
                f"  Provider returned:  {returned_model}\n"
                f"  Fallback api_base:  {self.fallback_llm.api_base or '<provider default>'}\n\n"
                "This means a proxy or routing layer intercepted the request\n"
                "and returned a response from a different model. Likely:\n"
                "  - OPENAI_BASE_URL points to a local proxy (LM Studio, vLLM)\n"
                "    that doesn't have the requested model and silently\n"
                "    answered with whatever model it does have loaded.\n"
                "  - An Azure OpenAI deployment ID is shadowing the requested\n"
                "    model name.\n\n"
                "Fix by EITHER:\n"
                "  a) Passing the fallback as a pre-built LLM instance with\n"
                "     an explicit api_base= that points at the right endpoint.\n"
                "  b) Unsetting OPENAI_BASE_URL (or equivalent env var) for\n"
                "     this run.\n\n"
                "Aborting before evaluation starts."
            )

class LLMNotConfiguredError(RuntimeError):
    """Raised when the Harness LLM cannot authenticate, reach the provider,"""


def _models_match(requested: str, returned: str) -> bool:
    """Fuzzy match between a requested model id and what the provider
    returned. Permissive on both sides — providers often strip the
    'openai/' prefix and add version dates / deployment IDs.

    Examples that should MATCH:
      openai/gpt-4.1-mini          ↔ gpt-4.1-mini-2025-04-14
      claude-haiku-4-5-20251001    ↔ claude-haiku-4-5
      anthropic/claude-sonnet-4-6  ↔ claude-sonnet-4-6

    Examples that should NOT match (the misrouting we're catching):
      openai/gpt-4.1-mini          ↔ gemma-4-e4b-it-mlx
      claude-haiku-4-5-20251001    ↔ gpt-4.1-mini
    """
    def _normalize(m: str) -> str:
        m = m.lower().strip()
        # Strip provider prefix.
        if "/" in m:
            m = m.split("/", 1)[1]
        # Strip common version-date suffixes (-YYYYMMDD or -YYYY-MM-DD).
        import re as _re
        m = _re.sub(r"-\d{8}$", "", m)
        m = _re.sub(r"-\d{4}-\d{2}-\d{2}$", "", m)
        return m

    a = _normalize(requested)
    b = _normalize(returned)
    # Match if either is a substring of the other (handles version drift
    # in both directions: requested 'haiku-4-5' vs returned 'haiku-4-5-20251001',
    # or requested 'gpt-4.1-mini' vs returned 'gpt-4.1-mini-2025-04-14').
    return a in b or b in a

def _compose_callbacks(
    *callbacks: Callable[[Event], None] | None,
) -> Callable[[Event], None]:
    real = [cb for cb in callbacks if cb is not None]

    def _fan(event: Event) -> None:
        for cb in real:
            with contextlib.suppress(Exception):
                cb(event)

    return _fan


# ──────────────────────────────────────────────────────────────────────
# Live Reporting helpers — convert a Report into the wire format
# the ProofAgent backend expects (POST /api/v1/runs/sync).
# All three helpers handle missing fields gracefully so that even a
# partial / failed Report can still be uploaded for debugging.
# ──────────────────────────────────────────────────────────────────────


def _report_to_sync_payload(
    report: Any,
    *,
    role: str,
    seed: int | None,
    harness_llm: str,
    business_case: str = "",
    goal: str = "",
    agent_model: str = "",
    consensus_strategy: str = "",
    metrics_active: list[str] | None = None,
    traps_used: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Top level fields + RICH metadata for the subscription-grade dashboard.

    The harness Report contains far more than the basic score: per-metric
    confidence + spread + severity, juror consensus log, warnings, total
    tokens, plus the eval's call context (role, business case, goal,
    consensus strategy, list of traps thrown). Surface ALL of it so the
    dashboard report is genuinely subscription-worthy.

    Defensive defaults — backend's /sync requires `started_at` (datetime),
    `final_score` (float), `certification` (str). If the report has None
    for any of them (early-fail eval, crash during scoring), substitute
    a safe default so the sync STILL lands.
    """
    from datetime import datetime, timezone

    cert = getattr(report, "certification", None)
    cert_str = cert.value if hasattr(cert, "value") else (str(cert) if cert else None)

    # started_at: prefer report's value, fall back to UTC now (which is
    # close-enough — the run has just completed, this is end-of-eval).
    started_at = getattr(report, "started_at", None)
    if started_at is None:
        started_at = datetime.now(timezone.utc).isoformat()
    elif hasattr(started_at, "isoformat"):
        started_at = started_at.isoformat()

    # Pull the full juror consensus log so the dashboard can show
    # per-juror disagreement, revote markers, confidence per metric.
    consensus_payload: dict[str, Any] = {}
    for metric_name, cons in (getattr(report, "consensus_log", {}) or {}).items():
        try:
            consensus_payload[metric_name] = cons.model_dump() if hasattr(cons, "model_dump") else dict(cons)
        except Exception:
            pass

    # All metadata reported by the harness (personas, models, etc.)
    metadata = dict(getattr(report, "metadata", {}) or {})

    # Per-metric confidence + severity (the harness reports per-metric
    # juror agreement strength + how serious any failure was).
    confidence = dict(getattr(report, "confidence", {}) or {})
    severity = {
        k: (v.value if hasattr(v, "value") else str(v))
        for k, v in (getattr(report, "severity", {}) or {}).items()
    }

    return {
        "started_at": started_at,
        "duration_seconds": float(getattr(report, "duration_seconds", 0) or 0),
        "seed": int(seed) if seed is not None else 0,
        "harness_llm": str(harness_llm or "unknown"),
        "agent_model": str(agent_model or getattr(report, "agent_model", "") or ""),
        "agent_name": str(role or "agent"),
        "final_score": float(getattr(report, "final_score", 0.0) or 0.0),
        "certification": cert_str or "NOT_CERTIFIED",
        "per_metric": dict(getattr(report, "per_metric", {}) or {}),
        # Rich subscription-grade metadata stored under config so the
        # backend's RunSyncRequest doesn't need a wider Pydantic schema
        # (config is a free-form dict already accepted).
        "config": {
            "role": str(role or ""),
            "business_case": str(business_case or ""),
            "goal": str(goal or ""),
            "harness_llm": str(harness_llm or ""),
            "agent_model": str(agent_model or ""),
            "consensus_strategy": str(consensus_strategy or ""),
            "metrics_active": list(metrics_active or []),
            "traps_used": list(traps_used or []),
            "confidence_per_metric": confidence,
            "severity_per_metric": severity,
            "consensus_log": consensus_payload,
            "warnings": list(getattr(report, "warnings", []) or []),
            "summary_text": str(getattr(report, "summary", "") or ""),
            "tokens_used": int(getattr(report, "tokens_used", 0) or 0),
            "harness_metadata": metadata,
        },
    }


def _transcript_to_payload(report: Any) -> list[dict[str, Any]]:
    """Turn list — converted to a JSON safe list of dicts."""
    out: list[dict[str, Any]] = []
    for t in getattr(report, "transcript", []) or []:
        if hasattr(t, "model_dump"):  # pydantic
            out.append(t.model_dump())
        elif hasattr(t, "__dict__"):
            out.append({k: v for k, v in t.__dict__.items() if not k.startswith("_")})
        elif isinstance(t, dict):
            out.append(t)
    return out


def _findings_to_payload(report: Any) -> list[dict[str, Any]]:
    """Quality findings — converted to a JSON safe list of dicts."""
    out: list[dict[str, Any]] = []
    for f in getattr(report, "findings", []) or []:
        if hasattr(f, "model_dump"):
            out.append(f.model_dump())
        elif isinstance(f, dict):
            out.append(f)
    return out
