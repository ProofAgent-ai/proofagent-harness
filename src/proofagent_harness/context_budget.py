"""Context-budget helpers — keep prompts inside the LLM's context window."""

from __future__ import annotations

from typing import Any


def _lm():
    """litellm, on first use.

    Same reasoning as `llm.py`: this module is reached transitively by the audit layer while merely
    READING a report (audit -> consensus -> juror -> here), so a module-level import put the whole
    provider stack behind report analysis. The model-window lookups below are the only uses.
    """
    import litellm

    return litellm


CHARS_PER_TOKEN = 4

DEFAULT_RESPONSE_RESERVE_TOKENS = 2048

FALLBACK_CONTEXT_TOKENS = 32_000

SAFETY_MARGIN_TOKENS = 512

def detect_context_tokens(model: str) -> int:
    """Look up the model's max input window via LiteLLM. Fall back to 32K.

    A model served by LM Studio is asked for the context it is ACTUALLY LOADED with: LM Studio reloads an idle
    model on demand at its default (8k), while its metadata still advertises the full window (262k). Measured:
    a run's attacker failed on 8 of 9 turns after such a reload, with no warning before the run started."""
    if str(model).startswith("lm_studio/"):
        loaded = _lm_studio_loaded_context(str(model).split("/", 1)[1])
        if loaded:
            return loaded
    try:
        _ll = _lm()
        info = _ll.model_info if hasattr(_ll, "model_info") else None
        if info is None:
            cost_map = getattr(_lm(), "model_cost", {}) or {}
            entry = cost_map.get(model) or cost_map.get(model.split("/")[-1])
            if entry and "max_input_tokens" in entry:
                return int(entry["max_input_tokens"])
        max_t = _lm().get_max_tokens(model)
        if max_t and max_t > 0:
            return int(max_t)
    except Exception:
        pass
    return FALLBACK_CONTEXT_TOKENS

def _lm_studio_loaded_context(model_id: str) -> int | None:
    """loaded_context_length from LM Studio's REST API; the default it would JIT-load with when not loaded."""
    import os

    base = os.environ.get("LM_STUDIO_API_BASE", "http://localhost:1234/v1").rstrip("/")
    base = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        import httpx

        d = httpx.get(f"{base}/api/v0/models/{model_id}", timeout=5).json()
        if d.get("state") == "loaded" and d.get("loaded_context_length"):
            return int(d["loaded_context_length"])
        return 8192  # not loaded: LM Studio will load it on demand with its default context
    except Exception:
        return None


def char_budget_for(
    model: str,
    *,
    response_reserve_tokens: int = DEFAULT_RESPONSE_RESERVE_TOKENS,
    safety_margin_tokens: int = SAFETY_MARGIN_TOKENS,
) -> int:
    """Return the per-prompt character budget for this model."""
    ctx = detect_context_tokens(model)
    available = max(1024, ctx - response_reserve_tokens - safety_margin_tokens)
    return available * CHARS_PER_TOKEN

def truncate_field(text: str, budget_chars: int, label: str = "field") -> tuple[str, bool]:
    """Trim a single field to fit `budget_chars`."""
    if text is None:
        return "", False
    if len(text) <= budget_chars or budget_chars <= 200:
        return text, len(text) > budget_chars

    head_share = int(budget_chars * 0.6)
    tail_share = budget_chars - head_share - 60
    if tail_share < 50:
        return text[:budget_chars], True
    head = text[:head_share]
    tail = text[-tail_share:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{label}: {omitted:,} chars omitted] ...\n\n{tail}", True

def truncate_transcript(
    turns: list[Any], budget_chars: int
) -> tuple[list[Any], int]:
    """Drop oldest turns until the transcript fits inside `budget_chars`."""
    if not turns:
        return [], 0

    def turn_chars(t: Any) -> int:
        return len(getattr(t, "question", "")) + len(getattr(t, "answer", "")) + 100

    total = sum(turn_chars(t) for t in turns)
    if total <= budget_chars:
        return list(turns), 0

    # NEVER DROP A TURN. Dropping the oldest turns hid the turns that matter most (the planted
    # identity, the first injection a later callback builds on) and the jury still voted on all
    # of them. Measured on a local-model run: 8 of 25 turns dropped. Instead every turn keeps its
    # head and tail, trimmed to an equal share of the budget, so each one is still judged.
    share = max(400, budget_chars // len(turns) - 100)
    out: list[Any] = []
    trimmed = 0
    for t in turns:
        q = str(getattr(t, "question", "") or "")
        a = str(getattr(t, "answer", "") or "")
        if len(q) + len(a) <= share:
            out.append(t)
            continue
        qa = max(150, int(share * 0.35))           # the attack: enough to see what was asked
        aa = max(250, share - min(len(q), qa))     # the reply: where the evidence is
        nq, _ = truncate_field(q, qa, "question")
        na, _ = truncate_field(a, aa, "answer")
        try:
            out.append(t.model_copy(update={"question": nq, "answer": na}))
        except AttributeError:
            out.append(t)
        trimmed += 1
    return out, trimmed
