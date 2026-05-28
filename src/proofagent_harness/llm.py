"""LLM wrapper — BYO LLM via LiteLLM."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import litellm

litellm.suppress_debug_info = True

litellm.drop_params = True

# Per-model parameter strip table — for newer Anthropic models that reject
# parameters older models accepted (e.g. Opus 4.7 deprecated `temperature`).
# Match by substring: any model id containing the key strips the listed params.
_DEPRECATED_PARAMS_BY_MODEL: dict[str, set[str]] = {
    "claude-opus-4-7": {"temperature"},
    "claude-opus-4-8": {"temperature"},  # forward-compat: assume same trend
    "gpt-5": {"temperature"},  # GPT-5.x family: only temperature=1 supported
    "o1-": {"temperature"},
    "o3-": {"temperature"},
    "o4-": {"temperature"},
}


def _strip_deprecated_params(model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop params known to be rejected by `model`. Returns a new dict."""
    out = dict(kwargs)
    for needle, drops in _DEPRECATED_PARAMS_BY_MODEL.items():
        if needle in model:
            for p in drops:
                out.pop(p, None)
    return out


def _is_deprecated_param_error(exc: Exception) -> str | None:
    """Detect 'param X is deprecated/unsupported' errors. Return param name or None.

    Covers Anthropic phrasing ("deprecated"), OpenAI phrasing ("does not support",
    "Unsupported value"), and generic ("not supported", "not allowed").
    """
    msg = str(exc).lower()
    triggers = ("deprecated", "not supported", "not allowed", "does not support", "unsupported value", "unsupported parameter")
    if not any(t in msg for t in triggers):
        return None
    for p in ("temperature", "top_p", "top_k", "seed", "max_tokens"):
        if f"`{p}`" in msg or f"'{p}'" in msg or f' {p} ' in msg:
            return p
    return None


@dataclass
class CompletionResult:
    """Outcome of a single LLM call."""

    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

@dataclass
class LLM:
    """Thin wrapper over LiteLLM for BYO model support.

    Optional cross-family fallback
    ──────────────────────────────
    Set ``fallback_llm=LLM(model="…")`` to enable automatic rescue when the
    primary call fails (empty response, JSON parse error, exception). The
    fallback receives the SAME ORIGINAL prompt — we never append the failed
    reply or an error message to the conversation (added in v0.4.2 to fix a
    compounding-prompt feedback loop that overflowed model context windows
    on long evals).

    The fallback is optional. Without one, the LLM raises
    :class:`LLMJSONStructureError` (for JSON failures) or :class:`LLMError`
    (for transport / empty-response failures) with an actionable message
    telling the user how to recover.
    """

    model: str = "claude-sonnet-4-6"
    temperature: float = 0.2
    # ── max_tokens: the MAX OUTPUT (generation) tokens the Harness LLM is
    # allowed to write per call. NOT the context window (input + output
    # combined — that's much larger, 200K-1M for frontier models).
    #
    # Bumped from 2048 → 8192 in v0.4.3 to fit long-context audit JSON.
    # At turns=50 with debate consensus, each juror call produces ~4000
    # tokens of output (50-entry per_turn_audit array + reasoning + score).
    # The pre-v0.4.3 default of 2048 truncated those responses mid-string,
    # which the JSON parser correctly rejected and which the new fallback
    # logic correctly classified as both-failed (since both primary AND
    # fallback hit the same 2048 cap).
    #
    # 8192 fits all common fallback models (Anthropic Haiku 8K, GPT-4.1-mini
    # 32K, Gemini 64K) and most quantized local models (LM Studio caps
    # silently to the underlying model's max if 8192 exceeds it). Setting
    # this higher than the model can actually generate is safe — providers
    # just cap to the hard limit.
    #
    # You only PAY for tokens actually generated, not the cap. So raising
    # max_tokens never increases cost — it only avoids truncation.
    max_tokens: int = 8192
    seed: int | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)
    # ── v0.4.2: native cross-family fallback ──
    # When set, failed calls (empty / non-JSON / exception) route to this
    # secondary LLM, which receives the ORIGINAL messages. Counters track
    # primary vs fallback spend separately so the cost-asymmetry story is
    # visible in the final report.
    fallback_llm: LLM | None = None
    # Optional callback invoked when fallback fires. Receives a dict with
    # keys: stage ('complete' | 'complete_json'), primary_model,
    # fallback_model, reason (exception type name + brief msg).
    # The harness wires this to its Event stream so on_event subscribers see
    # 'fallback_triggered' events in the live log.
    on_fallback: Callable[[dict[str, Any]], None] | None = None

    # Cumulative spend totals — shared across primary AND fallback calls.
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    call_count: int = 0
    # Per-source breakdown for asymmetric-cost reporting (added in v0.4.2).
    primary_call_count: int = 0
    primary_prompt_tokens: int = 0
    primary_completion_tokens: int = 0
    primary_cost_usd: float = 0.0
    fallback_call_count: int = 0
    fallback_prompt_tokens: int = 0
    fallback_completion_tokens: int = 0
    fallback_cost_usd: float = 0.0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """Single completion. Returns text + accounting.

        If ``self.fallback_llm`` is set and the primary call raises, the
        fallback receives the SAME messages (no error append) and its result
        is returned. Per-source token counters are updated separately.
        """
        try:
            r = await self._raw_complete(
                messages, system=system, temperature=temperature, max_tokens=max_tokens
            )
            self._track_primary(r)
            return r
        except LLMError as exc:
            if self.fallback_llm is None:
                raise
            # Fallback fires — log, route through fallback's own complete().
            self._notify_fallback(stage="complete", reason=type(exc).__name__, detail=str(exc)[:200])
            fb_r = await self.fallback_llm._raw_complete(
                messages, system=system, temperature=temperature, max_tokens=max_tokens
            )
            self._track_fallback(fb_r)
            return fb_r

    async def _raw_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """Underlying single-attempt LiteLLM call. No fallback, no retry.
        Raises LLMError on transport / API failure."""
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}, *msgs]

        call_kwargs: dict[str, Any] = dict(self.extra_kwargs)
        if self.seed is not None:
            call_kwargs.setdefault("seed", self.seed)
        call_kwargs["temperature"] = (
            temperature if temperature is not None else self.temperature
        )
        call_kwargs["max_tokens"] = max_tokens or self.max_tokens

        # Pre-strip params known-deprecated for this model (Opus 4.7 etc.).
        call_kwargs = _strip_deprecated_params(self.model, call_kwargs)

        try:
            resp = await litellm.acompletion(
                model=self.model,
                messages=msgs,
                **call_kwargs,
            )
        except Exception as exc:
            # Runtime fallback: if the provider returns a "param X deprecated"
            # error, drop X and retry once. Catches any future deprecation
            # before the model is added to _DEPRECATED_PARAMS_BY_MODEL.
            offender = _is_deprecated_param_error(exc)
            if offender and offender in call_kwargs:
                call_kwargs.pop(offender, None)
                _DEPRECATED_PARAMS_BY_MODEL.setdefault(self.model, set()).add(offender)
                try:
                    resp = await litellm.acompletion(
                        model=self.model,
                        messages=msgs,
                        **call_kwargs,
                    )
                except Exception as exc2:
                    raise LLMError(
                        f"LLM call failed for model={self.model!r}: {exc2}"
                    ) from exc2
            else:
                raise LLMError(
                    f"LLM call failed for model={self.model!r}: {exc}"
                ) from exc

        text = _extract_text(resp)
        prompt_tokens, completion_tokens = _extract_tokens(resp)
        cost = _estimate_cost(self.model, prompt_tokens, completion_tokens, resp)

        return CompletionResult(
            text=text,
            raw=_safe_dict(resp),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )

    # ── Per-source accounting ────────────────────────────────────────────
    # These methods update both the unified totals (for backwards compat
    # with existing report consumers) AND per-source breakdowns (new in
    # v0.4.2 for the asymmetric-cost story).

    def _track_primary(self, r: CompletionResult) -> None:
        self.call_count += 1
        self.total_tokens += r.prompt_tokens + r.completion_tokens
        self.total_cost_usd += r.cost_usd
        self.primary_call_count += 1
        self.primary_prompt_tokens += r.prompt_tokens
        self.primary_completion_tokens += r.completion_tokens
        self.primary_cost_usd += r.cost_usd

    def _track_fallback(self, r: CompletionResult) -> None:
        self.call_count += 1
        self.total_tokens += r.prompt_tokens + r.completion_tokens
        self.total_cost_usd += r.cost_usd
        self.fallback_call_count += 1
        self.fallback_prompt_tokens += r.prompt_tokens
        self.fallback_completion_tokens += r.completion_tokens
        self.fallback_cost_usd += r.cost_usd

    def _notify_fallback(self, *, stage: str, reason: str, detail: str = "") -> None:
        """Fire on_fallback callback (if any) AND print a short progress
        line to stdout so users always see fallback activity in their logs."""
        primary = self.model
        fb = getattr(self.fallback_llm, "model", "?") if self.fallback_llm else "?"
        # stdout progress line — visible regardless of on_event subscriber.
        print(
            f"[fallback] {primary} → {fb} · stage={stage} · reason={reason}"
            + (f": {detail}" if detail else ""),
            flush=True,
        )
        if self.on_fallback is not None:
            # Never crash the call path on a subscriber error.
            with contextlib.suppress(Exception):
                self.on_fallback({
                    "stage": stage,
                    "primary_model": primary,
                    "fallback_model": fb,
                    "reason": reason,
                    "detail": detail,
                })

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 0,  # DEPRECATED in v0.4.2 — see note below.
    ) -> dict[str, Any]:
        """Completion that must return parseable JSON.

        v0.4.2 BEHAVIOR CHANGE (bug fix)
        ─────────────────────────────────
        The pre-v0.4.2 implementation retried up to ``retries+1`` times on
        JSON parse failure and APPENDED the failed reply + error message to
        the conversation between retries. For long-context evals (50-turn
        debate transcripts), each retry compounded the prompt by ~30K
        tokens until even 200K-context fallbacks overflowed and produced
        garbage — a catastrophic feedback loop.

        v0.4.2 fixes this by taking a single attempt. If the primary fails:

          1. If ``self.fallback_llm`` is set → route the ORIGINAL messages
             (NOT the failed reply, NOT an error append) to the fallback.
          2. Else → raise :class:`LLMJSONStructureError` with an actionable
             message recommending: (a) a stronger model, (b) configure
             ``fallback_llm=``, or (c) lower turns / context-budget.

        v0.4.3 ADAPTIVE FALLBACK
        ─────────────────────────
        The fallback now uses a TIGHTER budget than the primary:

          - Reduced max_tokens (capped at ``min(primary_max_tokens, 4096)``)
          - Stricter system prompt ("BE EXTREMELY CONCISE, limit response
            to N chars MAXIMUM")

        Rationale: if the primary failed because it couldn't fit a complete
        JSON within its max_tokens budget (the v0.4.3 default of 8192 vs
        the bug-fix v0.4.2 default of 2048), asking the fallback for SHORTER
        output is the right move. The fallback still gets the ORIGINAL user
        messages (the v0.4.2 fix — no error-append) — only the SYSTEM
        instruction changes to push the model toward a more compact reply.

        The ``retries`` parameter is preserved for backwards compatibility
        but is now IGNORED (retrying with the same prompt is wasted spend,
        and retrying with an appended error was the bug).
        """
        effective_max_tokens = max_tokens or self.max_tokens
        # Primary char budget = roughly 3.5 chars/token * max_tokens. The
        # 3.5 (vs the more common 4) leaves slack for JSON syntax overhead
        # (brackets, quotes, commas) and tokens that are <4 chars (numbers,
        # short field names). It's a HINT to the model — not a hard cap —
        # but small local models often respect it better than they respect
        # the silent max_tokens cap.
        primary_char_budget = int(effective_max_tokens * 3.5)
        primary_sys = self._build_json_system_prompt(
            system, schema, char_budget=primary_char_budget, compact=False,
        )

        # ── Attempt 1: PRIMARY (full budget, normal "be valid JSON" prompt) ─
        try:
            r = await self._raw_complete(
                messages, system=primary_sys, temperature=temperature,
                max_tokens=effective_max_tokens,
            )
            try:
                data = _parse_json_loose(r.text)
                self._track_primary(r)
                return data
            except Exception as parse_exc:
                # Primary returned content but it isn't parseable JSON.
                # Fire fallback (if configured) or raise the actionable error.
                self._track_primary(r)  # still count the spend — they used tokens
                if self.fallback_llm is None:
                    raise LLMJSONStructureError(
                        model=self.model,
                        content_length=len(r.text),
                        parse_error=str(parse_exc),
                    ) from parse_exc
                self._notify_fallback(
                    stage="complete_json",
                    reason="json_parse_error",
                    detail=str(parse_exc)[:200],
                )
        except LLMError as transport_exc:
            # Primary raised before we could even attempt to parse — network
            # error, rate limit, auth failure, etc.
            if self.fallback_llm is None:
                raise
            self._notify_fallback(
                stage="complete_json",
                reason=type(transport_exc).__name__,
                detail=str(transport_exc)[:200],
            )

        # ── Attempt 2: FALLBACK (compact mode — only reachable if
        # fallback_llm is set) ──────────────────────────────────────────────
        # Fallback gets:
        #   1. SMALLER max_tokens: min(primary_max_tokens, 4096). If primary
        #      failed because it couldn't fit its output in the budget,
        #      asking for HALF that budget forces the model to be terse.
        #      For users who explicitly set max_tokens=2048 (cost-bound
        #      smoke tests), we never EXCEED their setting — only shrink.
        #   2. STRICTER system prompt: "BE EXTREMELY CONCISE, max N chars".
        #      Small models often respect the prompt hint better than the
        #      silent max_tokens cap.
        #   3. ORIGINAL messages: never the primary's failed reply, never
        #      an error message (the v0.4.2 bug fix stays intact).
        fallback_max_tokens = min(effective_max_tokens, 4096)
        fallback_char_budget = int(fallback_max_tokens * 3.5)
        fallback_sys = self._build_json_system_prompt(
            system, schema, char_budget=fallback_char_budget, compact=True,
        )
        fb_r = await self.fallback_llm._raw_complete(
            messages,  # ORIGINAL — no error append, no failed-reply leak
            system=fallback_sys,
            temperature=temperature,
            max_tokens=fallback_max_tokens,
        )
        try:
            data = _parse_json_loose(fb_r.text)
            self._track_fallback(fb_r)
            return data
        except Exception as fb_parse_exc:
            self._track_fallback(fb_r)
            # Both primary AND fallback failed to produce valid JSON. This
            # almost always means the prompt itself is broken (too long for
            # any model, contains invalid characters, etc.) — not a model
            # capability gap. Surface a different error so the user looks
            # at the prompt, not the model choice.
            raise LLMJSONStructureError(
                model=f"{self.model} (primary) + {self.fallback_llm.model} (fallback)",
                content_length=len(fb_r.text),
                parse_error=str(fb_parse_exc),
                both_failed=True,
            ) from fb_parse_exc

    @staticmethod
    def _build_json_system_prompt(
        user_system: str | None,
        schema: dict[str, Any] | None,
        *,
        char_budget: int,
        compact: bool,
    ) -> str:
        """Compose the system prompt for a complete_json call.

        ``compact=False`` (primary path): standard JSON-mode instruction +
        explicit character budget hint.

        ``compact=True`` (fallback path): same JSON-mode instruction PLUS a
        firm "BE EXTREMELY CONCISE" directive + a tighter character budget.
        Used when the primary already failed to produce valid JSON within
        the original budget — asking the fallback for a SHORTER reply
        usually beats asking for the same reply again.

        The user's original ``system`` (if any) is preserved verbatim and
        prepended — we only ADD instructions, never override the caller's
        framing.
        """
        if compact:
            base = (
                "Respond ONLY with valid JSON. No prose, no markdown fences.\n"
                "Your entire reply MUST be a complete, parseable JSON value.\n"
                "BE EXTREMELY CONCISE. Use the shortest possible reasoning. "
                "Drop verbose explanations.\n"
                f"Hard limit: keep your ENTIRE reply under {char_budget:,} "
                "characters. If the natural reply would exceed this, "
                "SUMMARIZE AGGRESSIVELY rather than truncate mid-sentence."
            )
        else:
            base = (
                "Respond ONLY with valid JSON. No prose, no markdown fences.\n"
                "Your entire reply MUST be a complete, parseable JSON value.\n"
                f"Aim to keep your reply under {char_budget:,} characters. "
                "If you cannot fit naturally, prefer to summarize rather "
                "than truncate mid-string."
            )
        if schema:
            base += f"\n\nJSON Schema:\n{json.dumps(schema, indent=2)}"
        return (user_system + "\n\n" + base) if user_system else base

class LLMError(RuntimeError):
    """Surface LLM problems with a tidy error type."""


class LLMJSONStructureError(LLMError):
    """Raised when an LLM can't produce parseable JSON for a structured-output
    call (juror scoring, planner trap selection, reporter findings, etc.).

    Carries an actionable message: which model failed, the parse error, and
    three concrete recommended fixes. Added in v0.4.2 to replace the
    cryptic ``Could not get valid JSON after 3 attempts: Unterminated
    string starting at: line 141 column 19`` error that gave users no
    direction on how to recover.
    """

    def __init__(
        self,
        *,
        model: str,
        content_length: int,
        parse_error: str,
        both_failed: bool = False,
        stage: str = "structured-output",
    ) -> None:
        self.model = model
        self.content_length = content_length
        self.parse_error = parse_error
        self.both_failed = both_failed
        self.stage = stage
        if both_failed:
            msg = (
                f"\n  Both the primary AND the fallback LLM failed to produce parseable JSON.\n"
                f"  Models tried: {model}\n"
                f"  Last parse error: {parse_error}\n"
                f"  Last response length: {content_length} chars\n"
                f"\n  This almost always means the PROMPT is the problem, not the model:\n"
                f"    - Lower --turns (try 8-20 instead of 50+)\n"
                f"    - Lower --context-budget (try 8000-16000 instead of 32000+)\n"
                f"    - Reduce trap library size or use --filter-traps to narrow scope\n"
                f"    - Increase max_tokens on your LLM wrapper to 4096+ so the JSON output isn't truncated\n"
            )
        else:
            msg = (
                f"\n  The LLM '{model}' could not produce parseable JSON at the {stage} stage.\n"
                f"  Output length: {content_length} chars · Parse error: {parse_error}\n"
                f"\n  This usually means the LLM is too small (or the prompt is too long for it)\n"
                f"  to follow structured-output instructions reliably. Three concrete fixes:\n"
                f"\n  1. Use a stronger model — Anthropic Haiku 4.5, GPT-4.1-mini, Claude\n"
                f"     Sonnet, or any 7B+ instruction-tuned LLM handle this reliably.\n"
                f"\n  2. Add a cross-family fallback LLM:\n"
                f"       Harness(llm='your-local-model',\n"
                f"               fallback_llm='anthropic/claude-haiku-4-5-20251001')\n"
                f"     Failed JSON-structured calls automatically route to the fallback,\n"
                f"     keeping the bulk of spend on the cheap local model.\n"
                f"\n  3. Reduce the prompt size: lower --turns (8-20 is usually enough\n"
                f"     for adversarial signal), lower --context-budget, or raise the\n"
                f"     LLM wrapper's max_tokens to 4096+ so the JSON output isn't truncated."
            )
        super().__init__(msg)

def _extract_text(resp: Any) -> str:
    try:
        choice = resp["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return str(content)
    except Exception:
        return str(resp)

def _extract_tokens(resp: Any) -> tuple[int, int]:
    try:
        usage = resp["usage"]
        return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    except Exception:
        return 0, 0

def _estimate_cost(
    model: str, prompt_tokens: int, completion_tokens: int, resp: Any = None
) -> float:
    """Use LiteLLM's cost lookup; fall back to a rough estimate or 0."""
    if resp is not None:
        try:
            return float(litellm.completion_cost(completion_response=resp))
        except Exception:
            pass
    try:
        return float(
            litellm.completion_cost(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )
    except Exception:
        pass
    return round((prompt_tokens / 1_000_000) * 0.15 + (completion_tokens / 1_000_000) * 0.60, 6)

def _safe_dict(resp: Any) -> dict[str, Any]:
    try:
        return dict(resp)
    except Exception:
        return {}

def _parse_json_loose(text: str) -> dict[str, Any]:
    """Parse JSON from a model reply, tolerating markdown fences and stray prose."""
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    if not s.startswith("{") and not s.startswith("["):
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start : end + 1]
    return json.loads(s)

def default_llm() -> LLM:
    """Return a sensible default LLM, picking up provider keys from env."""
    model = os.getenv("PROOFAGENT_LLM", "claude-sonnet-4-6")
    return LLM(model=model)

async def gather_with_concurrency(n: int, *coros: Any) -> list[Any]:
    """asyncio.gather with a bounded semaphore (for jury fan-out)."""
    sem = asyncio.Semaphore(n)

    async def _one(c: Any) -> Any:
        async with sem:
            return await c

    return await asyncio.gather(*[_one(c) for c in coros])
