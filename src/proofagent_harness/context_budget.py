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
    """Look up the model's max input window via LiteLLM. Fall back to 32K."""
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

    kept = list(turns)
    dropped = 0
    while kept and total > budget_chars:
        oldest = kept.pop(0)
        total -= turn_chars(oldest)
        dropped += 1

    return kept, dropped
