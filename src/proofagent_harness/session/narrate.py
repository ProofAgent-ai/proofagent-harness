"""One batched LLM call → a rich intent trajectory for the session recap.

Tier-1 (deterministic, 0 tokens) already flagged the risks. This adds the *human*
layer in a SINGLE call for the whole session (not one call per turn): for every turn
it produces a crisp intent, a one-line summary of what Claude did, and a short risk
note. Falls back to the raw prompt (still deterministic) when no LLM/key is available,
so the trajectory is always present — the LLM only makes it read nicely.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_SEV = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _mask(text: str) -> str:
    """Scrub key-like tokens and emails before a prompt leaves the machine —
    the same guarantee as the on-event intent preview. Nothing sensitive reaches
    the LLM or the dashboard."""
    t = " ".join((text or "").split())
    t = re.sub(r"[A-Za-z0-9_\-]{24,}", "…", t)
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", t)
    return t


# Conversational preamble to drop from the OFFLINE label so it lands on the real request, not
# the throat-clearing ("a couple of things ;", "ok", "also", "can you", "i want you to"). The
# LLM path ignores this entirely — it reads intent from the whole prompt.
_PREAMBLE = re.compile(
    r"^(?:a\s+c\w{1,3}ple\s+of\s+things|a\s+few\s+things|c\w{1,3}ple\s+of\s+things|some\s+things|"
    r"one\s+more\s+thing|quick\s+(?:one|thing|question)|ok(?:ay)?|hey|hi|so|also|and|then|"
    r"please|can\s+you|could\s+you|would\s+you|i\s+(?:just\s+)?(?:want|need|would\s+like)"
    r"(?:\s+you)?\s+to|let'?s|now)\b[\s,;:.\-—]*", re.I)


def actionize(prompt: str) -> str:
    """A cheap, LLM-free 'requested action' headline: the first meaningful clause of the prompt,
    trimmed to a short phrase and capped — DISTINCT from the full prompt, which still ships as
    evidence. ``--llm`` replaces this with a crisp model-derived imperative read from the WHOLE
    prompt; this only keeps the offline headline from being filler or the entire raw prompt.
    Already secret-masked by the caller (or via ``_mask``)."""
    t = " ".join((prompt or "").split())
    if not t:
        return ""
    # Peel off leading conversational preamble so "a couple of things ; add an about tab"
    # doesn't label as "A couple of things".
    prev = ""
    while t and t != prev:
        prev = t
        t = _PREAMBLE.sub("", t, count=1).lstrip(" ,;:.\t-—")
    if not t:  # the whole prompt was filler — fall back to the original
        t = " ".join((prompt or "").split())
    clause = re.split(r"[.;:\n]", t, maxsplit=1)[0].strip() or t
    words = clause.split()
    label = " ".join(words[:9]).rstrip(" ,-–—")
    if len(label) > 72:
        label = label[:72].rstrip() + "…"
    elif len(words) > 9:
        label += "…"
    return (label[:1].upper() + label[1:]) if label else t[:72]

_SYS = (
    "You label a coding-agent (Claude Code / Cursor) session for a governance dashboard. "
    "You receive the FULL session as an ordered list of turns — each with the developer's "
    "prompt, the tools/files the agent touched, and any deterministically-flagged risks. "
    "FIRST read the whole session to understand the developer's objective and how the turns "
    "connect; THEN label each turn IN THAT CONTEXT.\n"
    "For EACH turn return {i, intent, answer, risk_note}.\n"
    "`intent` = the REQUESTED ACTION in STANDARDIZED, canonical language: a short "
    "'<verb> <object> [<qualifier>]' imperative describing what the developer wants done in "
    "this turn, given the whole session. It is NOT a copy or paraphrase of the prompt's "
    "wording — NORMALIZE it so similar asks across the session read the same, and resolve "
    "vague words ('it', 'this', 'that') to the real target from context. 3-8 words, Title-case "
    "verb. Examples: 'Request dashboard code change', 'Restart evaluation run', 'Provision a "
    "clean developer account', 'Tune watch interval', 'Generate flagged test data'.\n"
    "`answer` = one plain line on what the agent actually did.\n"
    "`risk_note` = one short phrase explaining the flagged risk, or '' if none. Never invent a "
    "risk that is not in the input.\n"
    "Return strict JSON: {\"turns\": [ ... ]}."
)


def _group_turns(events: list, findings: list) -> list[dict[str, Any]]:
    """(prompt → the actions until the next prompt) with tokens + flagged risks."""
    by_seq: dict[int, list] = {}
    for f in findings:
        by_seq.setdefault(f.seq, []).append(f)
    turns: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for e in sorted(events, key=lambda e: e.seq):
        if e.action == "prompt" and (e.content or "").strip():
            cur = {"prompt": _mask(e.content or "")[:400], "ts": e.ts,
                   "tokens": 0, "actions": [], "risks": []}
            turns.append(cur)
        elif cur is not None:
            cur["tokens"] += int(e.tokens or 0)
            if e.target:
                cur["actions"].append(f"{e.tool or e.action}: {e.target}"[:120])
            for f in by_seq.get(e.seq, []):
                cur["risks"].append(f"{f.severity}:{f.category}")
    return turns


def probe_llm(model: str) -> tuple[bool, str]:
    """A tiny connectivity check for the harness LLM — one minimal call to confirm the key +
    model actually resolve BEFORE a watch loop starts, so a bad key/model surfaces up front
    instead of silently degrading to the deterministic labels. Returns (ok, error_message)."""
    if not model:
        return False, "no model set"
    try:
        import litellm
        litellm.completion(
            model=model, messages=[{"role": "user", "content": "ping"}],
            max_tokens=1, temperature=0,
        )
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:160]


def narrate_trajectory(
    events: list, findings: list, *, llm: str | None = None, max_turns: int = 60,
    usage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a trajectory: [{ts, intent, answer, tokens, risk, risk_note}] per turn.

    Deterministic base always present; a single LLM call (when ``llm``/PROOFAGENT_LLM
    is set) enriches ``intent`` / ``answer`` / ``risk_note``. When ``usage`` is passed it
    is filled with this call's token spend (``total_tokens``/prompt/completion) so the
    caller can surface the harness synthesis as the run's *eval tokens* — Tier-1 is free,
    but this one narration call is not, and 0 would misreport it."""
    # The MOST RECENT turns — a long-running session's latest activity is what a live watch
    # cares about, not its opening moves. (Taking the first N would narrate June while the
    # agent works in July.)
    turns = _group_turns(events, findings)[-max_turns:]
    if not turns:
        return []
    base = [{
        "ts": t["ts"],
        # `intent` is the derived requested-action (LLM overwrites it below). `prompt`
        # keeps the developer's actual (redacted) prompt as evidence — the two are shown
        # separately: the intent as the headline, the prompt in the evidence section.
        "intent": actionize(t["prompt"]),
        "prompt": t["prompt"][:400],
        "answer": "",
        "tokens": t["tokens"],
        "risk": max((r.split(":")[0] for r in t["risks"]),
                    key=lambda s: _SEV.get(s, 0), default=""),
        "risk_note": "",
    } for t in turns]

    model = llm or os.environ.get("PROOFAGENT_LLM")
    if not model:
        return base
    try:
        import litellm  # optional dep — never blocks the deterministic base
        payload = [{"i": i, "prompt": t["prompt"], "did": t["actions"][:8], "risks": t["risks"]}
                   for i, t in enumerate(turns)]
        msgs = [{"role": "system", "content": _SYS},
                {"role": "user", "content": json.dumps(payload)}]
        try:
            resp = litellm.completion(
                model=model, messages=msgs, temperature=0, num_retries=2,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            # Local OpenAI-compatible servers (LM Studio…) reject json_object —
            # retry once without it before degrading to the deterministic base.
            if "response_format" not in str(exc).lower():
                raise
            resp = litellm.completion(
                model=model, messages=msgs, temperature=0, num_retries=2,
            )
        # Loose parse — local models often fence the JSON in ```json blocks.
        from proofagent_harness.llm import _parse_json_loose
        data = _parse_json_loose(resp.choices[0].message.content)
        u = getattr(resp, "usage", None)
        if usage is not None and u is not None:
            usage["total_tokens"] = int(getattr(u, "total_tokens", 0) or 0)
            usage["prompt_tokens"] = int(getattr(u, "prompt_tokens", 0) or 0)
            usage["completion_tokens"] = int(getattr(u, "completion_tokens", 0) or 0)
        for row in data.get("turns", []):
            i = row.get("i")
            if isinstance(i, int) and 0 <= i < len(base):
                if row.get("intent"):
                    base[i]["intent"] = str(row["intent"])[:140]
                base[i]["answer"] = str(row.get("answer") or "")[:220]
                base[i]["risk_note"] = str(row.get("risk_note") or "")[:160]
    except Exception as exc:  # LLM is best-effort; deterministic base still governs —
        # but record WHY so the caller can surface it (missing key, bad model, rate limit)
        # instead of silently showing prompt-like intents with 0 eval tokens.
        if usage is not None:
            usage["error"] = f"{type(exc).__name__}: {exc}"[:180]
    return base
