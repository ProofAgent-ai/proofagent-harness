"""LLM wrapper — BYO LLM via LiteLLM."""

from __future__ import annotations

import asyncio
import json
import os
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
    """Thin wrapper over LiteLLM for BYO model support."""

    model: str = "claude-sonnet-4-6"
    temperature: float = 0.2
    max_tokens: int = 2048
    seed: int | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    total_cost_usd: float = 0.0
    total_tokens: int = 0
    call_count: int = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """Single completion. Returns text + accounting."""
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

        self.call_count += 1
        self.total_tokens += prompt_tokens + completion_tokens
        self.total_cost_usd += cost

        return CompletionResult(
            text=text,
            raw=_safe_dict(resp),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        """Completion that must return parseable JSON."""
        instructions = "Respond ONLY with valid JSON. No prose, no markdown fences."
        if schema:
            instructions += f"\n\nJSON Schema:\n{json.dumps(schema, indent=2)}"

        sys_prompt = (system + "\n\n" + instructions) if system else instructions

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            r = await self.complete(
                messages,
                system=sys_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                return _parse_json_loose(r.text)
            except Exception as exc:
                last_err = exc
                if attempt < retries:
                    messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                "Your previous reply was not valid JSON: "
                                f"{exc}. Please respond with JSON only."
                            ),
                        },
                    ]
        raise LLMError(f"Could not get valid JSON after {retries + 1} attempts: {last_err}")

class LLMError(RuntimeError):
    """Surface LLM problems with a tidy error type."""

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
