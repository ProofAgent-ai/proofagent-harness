"""How many adversarial turns this evaluation actually needs.

A turn count is a COVERAGE budget, not a precision knob: matched runs measured 8 turns at
22.1 pp spread and 15 turns at 28.2 pp, so more turns did not tighten the score. What a
short run costs is reach — the library spans 11 attack families, and a handful of turns
cannot visit them.

So the recommendation is driven by how much there is to cover:

    risk tier · frameworks declared · context weaknesses · attack surface · domains

Deterministic by construction. The recommendation is derived from the same inputs the
fingerprint already covers, so two runs of one command recommend the same number — a
recommendation that moved between runs would be worse than no recommendation, because a
user comparing two reports could not tell whether the exam or the agent had changed.
"""

from __future__ import annotations

from typing import Any

# A floor low enough to be useful for a smoke test, and a ceiling that keeps a runaway
# recommendation from turning one command into an unbounded bill.
MIN_TURNS = 8
MAX_TURNS = 40

# Enough to visit most of the 11 families once. Below this the run is a spot check.
BASELINE = 15


def _tier(profile: Any) -> str:
    for attr in ("tier", "risk_tier", "risk_classification"):
        value = getattr(profile, attr, None)
        if value:
            return str(value).lower()
    return ""


def recommend(
    *,
    governance_profile: Any = None,
    frameworks: list[str] | None = None,
    q_weights: dict[str, float] | None = None,
    agent_tools: list[str] | None = None,
    domains: list[str] | None = None,
    families_available: int = 11,
) -> tuple[int, list[str]]:
    """(recommended turns, reasons) — clamped to [MIN_TURNS, MAX_TURNS].

    Reasons are returned so the number is never a bare assertion: a user overriding it
    should be able to see what it was accounting for.
    """
    turns = float(BASELINE)
    reasons: list[str] = [f"baseline {BASELINE} to reach the {families_available} families"]

    tier = _tier(governance_profile)
    if any(k in tier for k in ("high", "prohibit", "unacceptable")):
        turns += 8
        reasons.append(f"+8 high-risk tier ({tier})")
    elif "limited" in tier or "medium" in tier:
        turns += 4
        reasons.append(f"+4 {tier} tier")

    # Each declared framework widens what has to be EVIDENCED, and a control with no
    # observation reads `not_evaluated` — which is honest but useless to the user who
    # asked for that framework.
    n_fw = len(frameworks or [])
    if n_fw > 4:
        turns += min(8, (n_fw - 4) * 2)
        reasons.append(f"+{min(8, (n_fw - 4) * 2)} for {n_fw} frameworks to evidence")

    # Every undefended area is somewhere the agent is running on its own training. Those
    # are the areas most worth spending turns on.
    exposed = sum(1 for w in (q_weights or {}).values() if w > 1.25)
    if exposed:
        turns += min(8, exposed)
        reasons.append(f"+{min(8, exposed)} for {exposed} exposed behaviour(s) in the context")

    # More tools is more consequential-action surface.
    n_tools = len(agent_tools or [])
    if n_tools >= 8:
        turns += 4
        reasons.append(f"+4 for {n_tools} tools")
    elif n_tools >= 4:
        turns += 2
        reasons.append(f"+2 for {n_tools} tools")

    if len(domains or []) >= 3:
        turns += 2
        reasons.append(f"+2 for {len(domains or [])} domains")

    rounded = round(turns)
    final = max(MIN_TURNS, min(MAX_TURNS, rounded))
    if final != rounded:
        reasons.append(f"clamped to {final}")
    return final, reasons


def describe(recommended: int, selected: int, adaptive: bool) -> str:
    """One line for the event stream and the report."""
    if adaptive:
        return f"adaptive: running {selected} turns (recommended {recommended})"
    if selected < recommended:
        return (
            f"running {selected} turns; {recommended} recommended for this "
            f"configuration — coverage will be partial"
        )
    if selected > recommended:
        return f"running {selected} turns; {recommended} would have sufficed"
    return f"running {selected} turns, matching the recommendation"
