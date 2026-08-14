"""Why a metric scored below 100% — derived from the verdicts that cost it the points.

WHY THIS EXISTS. A developer shown "Safety 97%" with nothing beside it has no way to know whether
three points went to a real weakness or to reviewer noise, and no way to act. Measured across a real
run, five of six behavioural metrics and three of seven context criteria scored below 100% with no
finding attached: the number was the whole story.

WHY IT IS DERIVED AND NOT WRITTEN. Every ingredient is already on the report. A check knows which
metrics it feeds and whether it is phrased positively or negatively; a verdict knows whether it was
observed, on which turn, with what quote, and how the panel split. So "which checks cost this metric
points" is arithmetic over data we already have — not a model's opinion, and not an invention. That
distinction is the whole point: a *fabricated* reason reads exactly like a real one and cannot be
checked, which is how an injected `risk_tier` line once ended up presented as a customer's prompt. A
*derived* reason can be recomputed from the same report by anyone.

WHAT COUNTS AS A DEDUCTION.
  * a NEGATIVE check observed        — the agent did the thing it should not have
  * a POSITIVE check not observed    — the agent omitted something required
  * a SPLIT panel                    — reviewers disagreed, so the check earned partial credit

The third is the one that was invisible: a metric can read 97% with every check nominally passing,
because a 2-of-3 vote earns 0.67 rather than 1.0. Saying so turns an unexplained three points into a
statement a reader can verify against the transcript.
"""

from __future__ import annotations

from typing import Any

#: Below this, a metric is treated as scoring full marks. Scores are floats on a 0-10 scale and a
#: run can land on 9.999 through rounding; claiming a deduction there would be noise.
_FULL_MARK_EPS = 0.05


def _all_checks() -> list[Any]:
    """The check library as objects. `load_checks()` returns `{id: CheckDef}`, and iterating it
    directly yields the KEYS — which silently produced an empty index and made every metric look
    like an unattributable aggregate deduction."""
    from proofagent_harness.checks import load_checks

    loaded = load_checks()
    return list(loaded.values()) if isinstance(loaded, dict) else list(loaded)


def _check_index() -> dict[str, Any]:
    return {str(getattr(c, "id", "")): c for c in _all_checks()}


def _cost(check: Any, verdict: dict[str, Any]) -> tuple[str, str] | None:
    """`(kind, phrase)` when this verdict cost points, else None.

    `kind` is one of `violation` / `omission` / `split`, so a caller can rank a proved violation
    above reviewer disagreement rather than presenting them as equivalent.
    """
    polarity = str(getattr(check, "polarity", "") or "").lower()
    observed = verdict.get("observed")
    total = int(verdict.get("votes_total") or 0)
    seen = int(verdict.get("votes_observed") or 0)
    title = str(getattr(check, "title", "") or getattr(check, "id", ""))
    turn = verdict.get("turn_index")
    where = f"turn {turn}" if turn is not None else "this run"

    if polarity == "negative" and observed is True:
        return "violation", f"{title} occurred on {where}"
    if polarity == "positive" and observed is False:
        return "omission", f"{title} did not happen on {where}"
    # A panel that did not agree earns partial credit even when the check passes outright.
    if total > 1 and 0 < seen < total:
        return "split", (
            f"{title}: {seen} of {total} reviewers agreed on {where}, "
            f"so it earned {seen / total:.2f} credit rather than full"
        )
    return None


_RANK = {"violation": 0, "omission": 1, "split": 2}


def metric_deductions(report: Any) -> dict[str, dict[str, Any]]:
    """For every behavioural metric below full marks: why, the proof, and the controls implicated.

    Returns `{metric: {"score_pct", "why", "proof", "checks", "controls", "kinds"}}`. A metric with
    no attributable verdict is still returned, with `why` naming that gap explicitly — the reader is
    told the points are unexplained rather than shown a bare number, and nothing is invented to fill
    it.
    """
    from proofagent_harness.compliance import FRAMEWORKS
    from proofagent_harness.crosswalk import SECURITY_FRAMEWORKS, controls_for_check

    def _get(o: Any, k: str, d: Any = None) -> Any:
        return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)

    per_metric = _get(report, "per_metric", {}) or {}
    verdicts = _get(report, "check_verdicts", []) or []
    index = _check_index()

    # verdicts grouped by the metrics their check feeds
    hits: dict[str, list[tuple[str, str, dict[str, Any], Any]]] = {}
    for v in verdicts:
        v = v if isinstance(v, dict) else dict(v)
        chk = index.get(str(v.get("check_id") or ""))
        if chk is None:
            continue
        cost = _cost(chk, v)
        if cost is None:
            continue
        kind, phrase = cost
        for m in (getattr(chk, "metrics", None) or []):
            hits.setdefault(str(m), []).append((kind, phrase, v, chk))

    out: dict[str, dict[str, Any]] = {}
    for metric, raw in per_metric.items():
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        if score >= 10.0 - _FULL_MARK_EPS:
            continue

        rows = sorted(hits.get(metric, []), key=lambda r: _RANK.get(r[0], 9))
        pct = round(score * 10)

        if not rows:
            # HONEST ABOUT THE GAP. The metric lost points and nothing on the report says which
            # observation cost them — usually an aggregate deduction with no per-check trace. Say
            # that, rather than attribute it to a check we did not verify.
            out[metric] = {
                "score_pct": pct,
                "why": (
                    f"Scored {pct}% and no individual observation on this run accounts for the "
                    f"{100 - pct} missing points, so the deduction is an aggregate one. "
                    f"Raise the turn count to put more of this metric under evidence."
                ),
                "proof": "",
                "checks": [],
                "controls": [],
                "kinds": [],
                "attributed": False,
            }
            continue

        # The proof is the quote from the worst-ranked verdict that has one — a real span of the
        # transcript, so the reader can find it.
        proof = ""
        for _kind, _phrase, v, _chk in rows:
            q = str(v.get("quote") or "").strip()
            if q:
                turn = v.get("turn_index")
                proof = f"turn {turn}: \"{q}\"" if turn is not None else f"\"{q}\""
                break

        # The controls the failing checks evidence. Scoped to the security frameworks for the same
        # reason the audit's control column is: every behaviour also implicates a dozen privacy and
        # sector regulations, and printing them all buries the refs a reviewer came for. The
        # regulatory reading is the C axis's job.
        controls: list[tuple[str, str]] = []
        seen_refs: set[str] = set()
        for _k, _p, _v, chk in rows:
            mapped = controls_for_check(str(getattr(chk, "id", ""))) or {}
            for fw_key in SECURITY_FRAMEWORKS:
                fw = FRAMEWORKS.get(fw_key) or {}
                by_id = {str(c.get("id")): c for c in fw.get("controls", [])}
                for ctrl_id in mapped.get(fw_key, ()):
                    c = by_id.get(str(ctrl_id))
                    if c is None:
                        continue
                    ref = str(c.get("ref") or ctrl_id)
                    if ref not in seen_refs:
                        seen_refs.add(ref)
                        controls.append((ref, str(c.get("title") or "")))

        lead = rows[0][1]
        more = len(rows) - 1
        out[metric] = {
            "score_pct": pct,
            "why": (
                f"Lost {100 - pct} points. {lead}"
                + (f", and {more} further observation{'s' if more != 1 else ''} cost credit." if more else ".")
            ),
            "proof": proof,
            "checks": [str(getattr(chk, "id", "")) for _k, _p, _v, chk in rows],
            "controls": controls,
            "kinds": sorted({k for k, _p, _v, _c in rows}),
            "attributed": True,
        }
    return out
