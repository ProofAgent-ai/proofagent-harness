"""Tier-2 escalation - the deep, LLM-backed evaluation that stays OFF unless
Tier-1 says it has to run.

Cost model (the whole point): Tier-1 screens every event for 0 tokens. Tier-2
is expensive, so it is *gated*:

  1. It fires ONLY when Tier-1 surfaced a finding at/above the escalation bar
     (default ``critical``). A clean or merely-noisy session never pays a token.
  2. When it does fire, it does NOT re-read the whole session. It aggregates the
     code the *critical* findings point at into one small slice and runs the
     existing artifact ``code`` rubric on that slice ONCE.

So Tier-2 cost scales with the number of critical code changes, not with session
length - a 24-hour session with two critical edits costs one small artifact eval.

Tier-2 is deliberately non-authoritative over the gate: criticals already block
via Tier-1, so a skipped/failed Tier-2 (e.g. no LLM key in CI) never changes the
release decision - it only deepens the explanation. The screen still blocks.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proofagent_harness.session.events import SessionEvent
from proofagent_harness.session.screen import ScreenFinding

_SEV_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# Only code-bearing actions are worth the artifact rubric; a bare `rm -rf` has
# no source to grade (Tier-1 already flagged it) so it never triggers spend.
_CODE_ACTIONS = {"write"}


def _bar(on: str) -> int:
    return _SEV_ORDER.get((on or "critical").lower(), 3)


def should_escalate(findings: list[ScreenFinding], *, on: str = "critical") -> bool:
    """True if any Tier-1 finding sits at/above the escalation bar."""
    bar = _bar(on)
    return any(_SEV_ORDER.get(f.severity, 0) >= bar for f in findings)


def escalating_seqs(findings: list[ScreenFinding], *, on: str = "critical") -> list[int]:
    bar = _bar(on)
    return sorted({f.seq for f in findings if _SEV_ORDER.get(f.severity, 0) >= bar})


def build_critical_slice(
    events: list[SessionEvent],
    findings: list[ScreenFinding],
    *,
    on: str = "critical",
    max_chars: int = 48_000,
) -> tuple[str, list[int]]:
    """Aggregate the CODE the escalating findings point at into one bounded blob
    for the artifact rubric. Pure + deterministic (no LLM) so the gating logic is
    unit-testable. Returns ``(slice_text, seqs_with_code)``.

    Only ``write`` events with content contribute source to grade; flagged
    commands are appended as context but do not, by themselves, create a slice.
    """
    bar = _bar(on)
    flags_by_seq: dict[int, set[str]] = {}
    for f in findings:
        if _SEV_ORDER.get(f.severity, 0) >= bar:
            flags_by_seq.setdefault(f.seq, set()).add(f.category)
    ev_by_seq = {e.seq: e for e in events}

    sections: list[str] = []
    commands: list[str] = []
    seqs_with_code: list[int] = []
    for seq in sorted(flags_by_seq):
        ev = ev_by_seq.get(seq)
        if ev is None:
            continue
        flags = ", ".join(sorted(flags_by_seq[seq]))
        if ev.action in _CODE_ACTIONS and ev.content.strip():
            seqs_with_code.append(seq)
            sections.append(
                f"## {ev.target or '(unknown)'}  (event {seq}) [flags: {flags}]\n"
                f"{ev.content.strip()}"
            )
        elif ev.action == "exec" and ev.target:
            commands.append(f"- (event {seq}) [{flags}] {ev.target[:200]}")

    if not sections:
        return "", []

    blob = (
        "# Critical code slice\n"
        "# Aggregated from the code changes the Tier-1 CRITICAL findings point at.\n"
        "# Grade this code for correctness, security, and safety.\n\n"
        + "\n\n".join(sections)
    )
    if commands:
        blob += "\n\n## Flagged commands run in the same session\n" + "\n".join(commands)
    return blob[:max_chars], seqs_with_code


@dataclass
class Tier2Outcome:
    """Result of the (maybe-skipped) Tier-2 pass. ``status`` is the whole story:
    ``not_triggered`` (Tier-1 below bar, 0 tokens), ``no_code`` (criticals had no
    gradable source), ``scored`` (artifact rubric ran), ``skipped``/``error``
    (LLM unavailable - gate unaffected)."""

    triggered: bool = False
    status: str = "not_triggered"
    reason: str = ""
    escalated_on: str = "critical"
    slice_seqs: list[int] = field(default_factory=list)
    slice_chars: int = 0
    final_score: float | None = None
    certification: str = ""
    dimensions: dict[str, float] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=lambda: {"total_tokens": 0})
    cost_summary: dict[str, Any] = field(
        default_factory=lambda: {"total_usd": 0.0, "primary_usd": 0.0, "fallback_usd": 0.0, "currency": "USD"}
    )
    llm: str = ""
    executive_summary: str = ""
    top_risk: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "status": self.status,
            "reason": self.reason,
            "escalated_on": self.escalated_on,
            "slice_seqs": self.slice_seqs,
            "slice_chars": self.slice_chars,
            "final_score": self.final_score,
            "certification": self.certification,
            "dimensions": self.dimensions,
            "finding_count": len(self.findings),
            "token_usage": self.token_usage,
            "cost_summary": self.cost_summary,
            "llm": self.llm,
            "executive_summary": self.executive_summary,
            "top_risk": self.top_risk,
        }


def assess_critical_slice(
    events: list[SessionEvent],
    findings: list[ScreenFinding],
    *,
    mode: str = "auto",
    on: str = "critical",
    llm: str | None = None,
    max_chars: int = 48_000,
    role: str = "an AI coding agent editing a production codebase",
) -> Tier2Outcome:
    """Run the cost-gated Tier-2 pass. ``mode``: ``auto`` (escalate only on
    criticals - the default), ``never`` (force Tier-1 only, always 0 tokens), or
    ``always`` (force the artifact rubric even without a critical - opt-in deep
    audit). Never raises: an LLM failure is captured as ``status='error'`` so the
    Tier-1 upload + gate proceed unchanged."""
    out = Tier2Outcome(escalated_on=on)
    if mode == "never":
        out.status = "disabled"
        return out
    if mode != "always" and not should_escalate(findings, on=on):
        out.status = "not_triggered"
        return out

    out.triggered = True
    slice_text, seqs = build_critical_slice(events, findings, on=on, max_chars=max_chars)
    out.slice_seqs = seqs
    out.slice_chars = len(slice_text)
    if not slice_text:
        out.status = "no_code"
        out.reason = "Escalation fired but the flagged events carried no gradable source code."
        return out

    # Lazy imports: the heavy artifact path is only pulled in when we actually
    # spend. Keeps Tier-1 import-light and `--assess never` dependency-free.
    try:
        import warnings

        from proofagent_harness import AgentArtifact, Harness
        from proofagent_harness.governance import _build_cost_summary, _split_token_usage

        art = AgentArtifact(generated_artifact=slice_text, type="code")
        with warnings.catch_warnings():  # keep the session scorecard clean
            warnings.simplefilter("ignore")
            report = Harness(mode="artifact", llm=llm, verbose=False).evaluate(
                artifact=art, role=role,
                business_case="Governing a live AI coding-agent session; only the "
                              "critical-flagged code is under review.",
            )
    except Exception as exc:  # degrade, never break the Tier-1 gate
        out.status = "error"
        out.reason = f"{type(exc).__name__}: {exc}"[:300]
        out.llm = llm or "(default)"
        return out

    out.status = "scored"
    out.llm = llm or "(default)"
    out.final_score = round(float(getattr(report, "final_score", 0.0)), 1)
    cert = getattr(report, "certification", None)
    out.certification = getattr(cert, "value", str(cert or ""))
    out.dimensions = {k: round(float(v), 1) for k, v in (getattr(report, "per_metric", {}) or {}).items()}
    out.executive_summary = str(getattr(report, "executive_summary", "") or "")
    out.top_risk = str(getattr(report, "top_risk", "") or "")
    out.token_usage = _split_token_usage(report)
    out.cost_summary = _build_cost_summary(report)
    out.findings = [
        {
            "title": f"[code] {getattr(f, 'headline', '')}"[:300],
            "description": (
                f"{getattr(f, 'detail', '')}"
                + (f"\n\nRecommendation: {f.recommendation}" if getattr(f, "recommendation", "") else "")
            ),
            "severity": getattr(getattr(f, "severity", None), "value", "medium"),
            "finding_type": "quality_issue",  # never a block_rule; Tier-1 owns the gate
            "turn_index": None,
            "evidence": {"tier": 2, "metric": getattr(f, "metric", ""), "source": "artifact_code_rubric"},
        }
        for f in (getattr(report, "findings", []) or [])
    ]
    return out
