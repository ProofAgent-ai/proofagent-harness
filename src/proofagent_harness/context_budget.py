"""Context-budget helpers — keep prompts inside the LLM's context window.

Strategy:
    1. At Harness construction, detect the model's max input window via
       LiteLLM's metadata (or fall back to a conservative default).
    2. Reserve some of that window for the response itself.
    3. Per-prompt, run a coarse char-budget check and trim to fit:
         - Transcripts shrink by dropping oldest turns first (recent turns
           carry the most signal).
         - Single fields (knowledge corpus, agent answers, retrievals) trim
           with a "head + ... [N chars omitted] ... + tail" pattern so
           jurors still see both ends of the document.
    4. If trimming happens, emit a `context_truncated` event so the user
       knows the eval was running tight.

Char-vs-token note:
    We use a 4-chars-per-token rough estimate. This is conservative for
    English (typically 3.5-4) and pessimistic for code/markup (denser).
    Good enough for this defensive layer; the LLM will still error if we
    underestimate, which is the right failure mode.
"""

from __future__ import annotations

from typing import Any

import litellm

# 4 chars/token is a safe rough estimate. We err on the side of trimming early.
CHARS_PER_TOKEN = 4

# Default reserve for the response — keep some room for the agent to write.
DEFAULT_RESPONSE_RESERVE_TOKENS = 2048

# If we can't detect the model's window, assume 32K tokens (covers GPT-4o-mini,
# llama 3 8B-instruct, claude haiku, etc.).
FALLBACK_CONTEXT_TOKENS = 32_000

# Per-call safety margin (system prompt overhead, message wrappers, etc.).
SAFETY_MARGIN_TOKENS = 512


def detect_context_tokens(model: str) -> int:
    """Look up the model's max input window via LiteLLM. Fall back to 32K."""
    try:
        # LiteLLM exposes per-model metadata. The exact key varies by version.
        info = litellm.model_info if hasattr(litellm, "model_info") else None
        if info is None:
            cost_map = getattr(litellm, "model_cost", {}) or {}
            entry = cost_map.get(model) or cost_map.get(model.split("/")[-1])
            if entry and "max_input_tokens" in entry:
                return int(entry["max_input_tokens"])
        max_t = litellm.get_max_tokens(model)
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
    """Return the per-prompt character budget for this model.

    `(context_window - response - margin) * CHARS_PER_TOKEN`
    """
    ctx = detect_context_tokens(model)
    available = max(1024, ctx - response_reserve_tokens - safety_margin_tokens)
    return available * CHARS_PER_TOKEN


def truncate_field(text: str, budget_chars: int, label: str = "field") -> tuple[str, bool]:
    """Trim a single field to fit `budget_chars`.

    Preserves head + tail so the juror / conductor can still see what kind of
    content it is and how it ends. Returns (text, was_truncated).
    """
    if text is None:
        return "", False
    if len(text) <= budget_chars or budget_chars <= 200:
        return text, len(text) > budget_chars

    head_share = int(budget_chars * 0.6)
    tail_share = budget_chars - head_share - 60  # leave room for the marker
    if tail_share < 50:
        return text[:budget_chars], True
    head = text[:head_share]
    tail = text[-tail_share:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{label}: {omitted:,} chars omitted] ...\n\n{tail}", True


def truncate_transcript(
    turns: list[Any], budget_chars: int
) -> tuple[list[Any], int]:
    """Drop oldest turns until the transcript fits inside `budget_chars`.

    Returns (turns_kept, n_dropped). Recent turns carry the most signal — they're
    the result of escalation. Older turns get dropped first.
    """
    if not turns:
        return [], 0

    def turn_chars(t: Any) -> int:
        # Conservative — we re-render in juror's _build_user_message; this
        # is a budget estimate.
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
