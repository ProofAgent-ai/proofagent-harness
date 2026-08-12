"""The reporter writes prose. It does not decide.

An evaluation report's verdict has to be a policy evaluation over the findings. It was a
language model's answer to a policy question: the prompt asked for `production_ready`, handed
the model the rule to apply ("if a critical finding exists, production_ready must be
'blocked'"), and accepted whatever came back after an enum check. The deterministic verdict
existed the whole time and was demoted to a fallback for when the call failed.

Separately, the score each finding was described to that model with was recovered by regexing
a percentage out of its rendered headline, returning 0.0 on no match — so a change to the
headline format would have reported every finding as 0% with no error anywhere.

These tests pin both. They are cheap to satisfy and expensive to lose.
"""

from __future__ import annotations

from proofagent_harness.agents import reporter as rep
from proofagent_harness.schemas import Finding, Severity


def _code_only(src: str) -> str:
    """Source with comment lines removed, so a guard reads code and not its explanation."""
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


def _finding(metric: str, headline: str, severity: Severity = Severity.FAIL) -> Finding:
    return Finding(metric=metric, headline=headline, severity=severity,
                   detail="d", recommendation="r")


# ── the score is read, not recovered from a rendered string ─────────────────


def test_a_finding_score_comes_from_the_authoritative_map() -> None:
    f = _finding("safety", "Safety: 20% — critical")
    # The headline says 20%; the map says 8.0. The map wins.
    assert rep._finding_score(f, {"safety": 8.0}) == 8.0


def test_a_reworded_headline_no_longer_zeroes_the_score() -> None:
    """The regression this pins: the score was regexed out of the headline, so a format
    change reported every finding as 0% — silently, to the model doing the synthesis."""
    for headline in ("Safety — strong", "Safety: excellent", "", "Safety 8 of 10"):
        f = _finding("safety", headline)
        assert rep._finding_score(f, {"safety": 8.0}) == 8.0, headline


def test_an_unscored_metric_reads_zero_rather_than_guessing() -> None:
    assert rep._finding_score(_finding("safety", "Safety: 90%"), {}) == 0.0


def test_the_score_helper_requires_the_map() -> None:
    """Signature guard: a one-argument call would mean someone reintroduced the recovery."""
    import inspect

    params = list(inspect.signature(rep._finding_score).parameters)
    assert params == ["f", "per_metric"]


# ── the verdict is computed, never taken from the model ─────────────────────


def _synthesis(monkeypatch, llm_payload: dict) -> tuple[str, str, str]:
    """Run the executive synthesis with the LLM returning `llm_payload`."""
    captured: dict[str, object] = {}

    class _LLM:
        def complete_json(self, *a, **k):
            captured["prompt"] = " ".join(str(x) for x in a) + str(k)
            return llm_payload

    monkeypatch.setattr(rep, "_emit", lambda *a, **k: None, raising=False)
    # Every parameter passed explicitly. An earlier version omitted `severity`, so the call
    # raised TypeError and the test caught it and returned — passing while asserting nothing.
    return rep._generate_executive_synthesis(
        state={"llm": _LLM()},
        final_score=9.5,
        certification="GOLD",
        per_metric={"safety": 2.0},
        severity={"safety": Severity.CRITICAL},
        findings=[_finding("safety", "Safety: 20% — critical", Severity.CRITICAL)],
        consensus={},
    )


def test_a_critical_finding_blocks_whatever_the_model_says(monkeypatch) -> None:
    """The exact failure mode: a GOLD certification, a 9.5 score, and one critical finding.
    The model claiming `ready` must not survive contact with the policy."""
    summary, prod_ready, top_risk = _synthesis(monkeypatch, {
        "executive_summary": "All good, ship it.",
        "production_ready": "ready",          # the model's opinion
        "top_risk": "Nothing of concern.",    # the model's ranking
    })
    assert prod_ready == "blocked", "a critical finding decides this, not the model"
    assert top_risk != "Nothing of concern.", "the risk is ranked from the findings"
    assert summary == "All good, ship it.", "prose is still the model's job"


def test_the_prompt_no_longer_asks_the_model_to_decide() -> None:
    """Source-level guard, because it survives any refactor of the call plumbing. Asking for
    a value that is then discarded spends tokens and tells the model it is deciding."""
    import inspect

    # COMMENTS STRIPPED FIRST. The code carries an explanation of what was removed, which
    # quotes the old prompt verbatim — a guard that matched it would fail on its own
    # documentation and teach the next person to delete the comment.
    src = _code_only(inspect.getsource(rep._generate_executive_synthesis))
    ask = src.split("# Your task")[1] if "# Your task" in src else src
    assert '"production_ready": one of' not in ask
    assert '"top_risk": ONE sentence' not in ask
    assert "production_ready must be" not in ask, "the policy rule was handed to the model"
    assert "Do NOT state whether the agent is production-ready" in ask


def test_the_response_schema_asks_for_prose_only() -> None:
    import inspect

    src = _code_only(inspect.getsource(rep._generate_executive_synthesis))
    schema = src[src.index("schema = {"):]
    assert '"executive_summary"' in schema
    assert '"production_ready"' not in schema, "still in the response contract"
    assert '"top_risk"' not in schema


def test_the_model_output_is_read_for_prose_only() -> None:
    """The accept path must not read a verdict out of the response at all."""
    import inspect

    src = _code_only(inspect.getsource(rep._generate_executive_synthesis))
    assert 'data.get("production_ready")' not in src
    assert 'data.get("top_risk")' not in src
    assert 'data.get("executive_summary")' in src
