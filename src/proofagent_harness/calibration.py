"""Run calibration — establishes the scoring policy before an evaluation starts.

Runs as a phase, never as a user-facing option. It resolves three things and hands
them to the graph as a `Calibration`:

  * a fingerprint of everything that defines this evaluation,
  * whether a prior transcript for that fingerprint can be reused,
  * how many scoring passes the jury and the compliance assessor should take.

Maintenance notes:
  * `Calibration` is the only object that crosses into the graph (state key
    "calibration"). Adding a field here means adding it to `to_metadata()` too, or
    it will not reach the report.
  * Everything on disk lives under PROOFAGENT_HOME (default ~/.proofagent) and is a
    cache: deleting it is always safe, and a cold cache only costs extra calls.
  * `measure_jury` results are keyed on the harness-LLM configuration alone, so they
    are reused across agents. `measure_agent` results are not cached — they are a
    property of the agent under test.
  * PROOFAGENT_CALIBRATION=0 disables the whole phase (falls back to single-pass
    scoring, fresh transcript). Use it to bisect a suspected calibration problem.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

DETERMINISTIC = "deterministic"
STABLE = "stable"
VOLATILE = "volatile"

# Target agreement between two scoring passes of the same input, on the 0-10 metric
# scale. 0.1 here is 1% on the reported 0-100 scale.
TOLERANCE = 0.1

# Pass counts the ladder may climb to. DAMPING IS OFF BY DEFAULT because it was measured
# and did not pay: a set scored at K=5 showed a 28.2 pp spread on the behavioural axis
# while a comparable set at K=1 showed 26.6 pp — the cheap set was tighter. Repeat
# passes damp the SCORER, and the variance that survives is the AGENT behaving
# differently between runs, which averaging cannot touch. K=5 tripled jury calls
# (18 -> 90 per run, $0.29 -> $0.85) for no measurable gain.
#
# The residual is still MEASURED and reported — knowing the scorer's spread is worth the
# one cached measurement — it just no longer multiplies the work. Set
# PROOFAGENT_JURY_DAMPING=1 to re-enable the ladder.
_LADDER_OFF = (1,)
_LADDER_ON = (1, 3, 5)


def damping_enabled() -> bool:
    return os.environ.get("PROOFAGENT_JURY_DAMPING", "0") == "1"


def _ladder() -> tuple[int, ...]:
    return _LADDER_ON if damping_enabled() else _LADDER_OFF
# Probe budget for the agent-volatility check. Deliberately small: the question is
# BINARY — does this agent answer the same prompt differently — and detecting that needs
# far less evidence than estimating how much. The earlier 3x3 spent 9 agent calls per
# cold run to reach the same verdict 2x2 reaches in 4. The probe stays because it is the
# single most useful diagnostic the harness produces: it tells a user their score moved
# because their AGENT moved, not because the harness is unreliable.
_REPLIES = 2
_MIN_PROBES, _MAX_PROBES = 2, 3
_PROBE_SHARE = 0.15
_SCHEMA_VERSION = 3
# Environment variables that change how the AGENT UNDER TEST behaves. Anything listed
# here becomes part of the fingerprint. Extend it when an example or adapter starts
# reading a new knob — a missed one silently allows a wrong-transcript replay.
AGENT_ENV_KEYS: tuple[str, ...] = (
    "AGENT_LLM", "AGENT_TEMPERATURE", "AGENT_MODEL", "AGENT_SEED",
    "AGENT_TOP_P", "AGENT_MAX_TOKENS", "AGENT_BASE_URL",
)


def agent_env() -> dict[str, str]:
    """The agent-affecting environment, as it is at this moment."""
    return {k: os.environ[k] for k in AGENT_ENV_KEYS if k in os.environ}


def _home() -> Path:
    return Path(os.environ.get("PROOFAGENT_HOME") or (Path.home() / ".proofagent"))


def enabled() -> bool:
    return os.environ.get("PROOFAGENT_CALIBRATION", "1") != "0"


@dataclass
class Calibration:
    """The scoring policy for one run."""

    fingerprint: str = ""
    transcript_source: str = "generated"
    agent_class: str = STABLE
    agent_determinism: float = 1.0
    jury_residual: float | None = None   # None = not measured, NOT stable
    k_metrics: int = 1
    k_compliance: int = 1
    replay: list[Any] = field(default_factory=list)
    context_engineering: dict[str, Any] = field(default_factory=dict)
    """The context assessment from the run that produced the stored transcript.

    Cached for the same reason the transcript is. Grading the context is an LLM call on a
    fixed artifact and is NOT deterministic: two scorings of one transcript put
    `grounding_sufficiency` at 70% and 50%, and reworded every finding. Because the
    assessment now weights the behavioural score and is rendered into every juror prompt,
    that wobble moved `hallucination_resistance` 16.1 pp on an IDENTICAL transcript.
    Reusing it makes the replay exact, and saves the call."""
    notes: list[str] = field(default_factory=list)

    @property
    def replaying(self) -> bool:
        return self.transcript_source == "replayed" and bool(self.replay)

    def turn_at(self, index: int) -> Any | None:
        """The stored turn for `index`, or None when there is nothing to reuse."""
        if not self.replaying or index >= len(self.replay):
            return None
        return self.replay[index]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "transcript_source": self.transcript_source,
            "agent_class": self.agent_class,
            "agent_determinism": round(float(self.agent_determinism), 3),
            "jury_residual": (None if self.jury_residual is None
                              else round(float(self.jury_residual), 3)),
            "scoring_passes": self.k_metrics,
            "compliance_passes": self.k_compliance,
        }


# ── fingerprint ────────────────────────────────────────────────────────────────

def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


def _path_digest(path: Any) -> str:
    """Content digest of a file or a directory tree. Empty string when absent."""
    if not path:
        return ""
    p = Path(str(path))
    if not p.exists():
        return ""
    h = hashlib.sha256()
    files = sorted(p.rglob("*")) if p.is_dir() else [p]
    for f in files:
        if f.is_file() and not f.name.startswith("."):
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:32]


def fingerprint(
    *,
    agent_source: Any = None,
    context: Any = None,
    knowledge: Any = None,
    traps: list[str] | None = None,
    turns: int = 0,
    metrics: list[str] | None = None,
    consensus: str = "",
    personas: list[str] | None = None,
    llm: str = "",
    fallback_llm: str = "",
    governance: Any = None,
    seed: Any = None,
    agent_env: dict[str, str] | None = None,
) -> str:
    """Stable id for an evaluation. Any change to what is under test changes it."""
    return _digest(
        _SCHEMA_VERSION,
        # The seed drives trap SELECTION, so it must be part of the identity — without
        # it a run with a new seed would replay a transcript built from a different one.
        seed,
        _path_digest(agent_source),
        _context_digest(context),
        _path_digest(knowledge),
        list(traps or []),
        turns,
        sorted(metrics or []),
        consensus,
        sorted(personas or []),
        llm,
        fallback_llm,
        _governance_digest(governance),
        # The agent's RUNTIME config, not just its source. An agent file that reads its
        # model or temperature from the environment is a different agent at a different
        # setting, and hashing only the source made AGENT_TEMPERATURE=0.5 collide with
        # 0.2 — so a volatile run could be handed a transcript from a calm one.
        dict(sorted((agent_env or {}).items())),
    )[:16]


def _context_digest(context: Any) -> str:
    if context is None:
        return ""
    bits: list[str] = []
    for attr in ("system_prompt", "role", "goal", "business_case"):
        bits.append(str(getattr(context, attr, "") or ""))
    tools = getattr(context, "tools", None)
    bits.append(json.dumps(tools, sort_keys=True, default=str) if tools else "")
    memory = getattr(context, "memory", None)
    bits.append(json.dumps(memory, sort_keys=True, default=str) if memory else "")
    return _digest(*bits)[:32]


def _governance_digest(profile: Any) -> str:
    if profile is None:
        return ""
    return _digest(
        str(getattr(profile, "name", "")),
        str(getattr(profile, "risk_level", "")),
        getattr(profile, "controls", {}) or {},
        sorted(getattr(profile, "frameworks", []) or []),
    )[:32]


# ── transcript reuse ───────────────────────────────────────────────────────────

def _store() -> Path:
    return _home() / "transcripts"


def load_transcript(
    fp: str, search: list[Any] | None = None,
) -> tuple[list[dict], dict] | None:
    """Stored turns for `fp` plus what was measured about the agent that produced them.

    The agent measurement travels with the transcript so a replay keeps the same
    scoring policy as the run that generated it — otherwise the compliance pass count
    would differ between a cold and a warm run of an identical fingerprint."""
    if not fp:
        return None
    cached = _store() / f"{fp}.json"
    if cached.is_file():
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            turns = data.get("transcript") or []
            if turns:
                measured = dict(data.get("agent") or {})
                measured["context_engineering"] = dict(
                    data.get("context_engineering") or {}
                )
                return turns, measured
        except Exception:
            pass
    for candidate in _report_candidates(search):
        try:
            data = json.loads(Path(candidate).read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("metadata") or {}
        if meta.get("fingerprint") == fp and data.get("transcript"):
            return data["transcript"], {
                "agent_class": meta.get("agent_class"),
                "agent_determinism": meta.get("agent_determinism"),
                # A shared report carries its own context assessment; reusing it is what
                # lets a colleague reproduce the score without re-grading the prompt.
                "context_engineering": dict(data.get("context_engineering") or {}),
            }
    return None


def _report_candidates(search: list[Any] | None) -> list[Path]:
    out: list[Path] = []
    for entry in search or []:
        p = Path(str(entry))
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.glob("*.json"))[:40])
    return out


def save_transcript(
    fp: str, turns: list[Any], agent: dict | None = None,
    context: dict | None = None,
) -> None:
    """Best effort — a failed write only means the next run recomputes."""
    if not fp or not turns:
        return
    try:
        _store().mkdir(parents=True, exist_ok=True)
        payload = [t if isinstance(t, dict) else _dump(t) for t in turns]
        (_store() / f"{fp}.json").write_text(
            json.dumps({
                "fingerprint": fp, "transcript": payload, "agent": dict(agent or {}),
                # The context assessment travels with the transcript so a replay scores
                # against the same grade, not a freshly re-asked one.
                "context_engineering": dict(context or {}),
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def _dump(obj: Any) -> dict:
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn(mode="json") if attr == "model_dump" else fn()
            except TypeError:
                return fn()
    return dict(getattr(obj, "__dict__", {}) or {})


# ── jury agreement ─────────────────────────────────────────────────────────────

def _profiles() -> Path:
    return _home() / "profiles"


def jury_key(
    *, llm: str, fallback_llm: str, consensus: str,
    personas: list[str] | None, metrics: list[str] | None,
) -> str:
    """Identifies a scoring configuration, independent of the agent under test."""
    return _digest(
        _SCHEMA_VERSION, llm, fallback_llm, consensus,
        sorted(personas or []), sorted(metrics or []),
    )[:16]


def _load_profile(key: str) -> tuple[float, int] | None:
    f = _profiles() / f"{key}.json"
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        r = d["jury_residual"]
        return (None if r is None else float(r)), int(d["k_metrics"])
    except Exception:
        return None


def _save_profile(key: str, residual: float | None, k: int) -> None:
    try:
        _profiles().mkdir(parents=True, exist_ok=True)
        (_profiles() / f"{key}.json").write_text(
            json.dumps({
                "jury_residual": (None if residual is None else round(residual, 4)),
                "k_metrics": k,
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


async def measure_jury(score_once: Any, *, key: str) -> tuple[float | None, int]:
    """Smallest pass count whose repeat spread lands within TOLERANCE.

    `score_once` is an awaitable returning {metric: score} for a fixed input; it is
    called repeatedly on IDENTICAL input, so any spread is the scorer's own.

    Returns (residual, passes). A residual of None means the spread could NOT be
    measured — never that the scorer is stable. That distinction matters: an earlier
    version returned 0.0 on a failed measurement, which cached a permanent
    "perfectly stable, one pass" verdict for a scorer nobody had ever observed. An
    unmeasured result is also NOT cached, so the next run tries again.
    """
    cached = _load_profile(key)
    if cached is not None:
        return cached

    samples: list[dict[str, float]] = []
    residual: float | None = None
    chosen = 1
    for k in _ladder():
        while len(samples) < max(2, k + 1):
            got = await score_once()
            if not got:
                break
            samples.append(got)
        if len(samples) < 2:
            return None, 1
        residual = _spread(samples, k)
        chosen = k
        if residual <= TOLERANCE:
            break

    _save_profile(key, residual, chosen)
    return residual, chosen


def _spread(samples: list[dict[str, float]], k: int) -> float:
    """Worst per-metric disagreement between two k-pass aggregates."""
    if len(samples) < 2:
        return 0.0
    if k <= 1:
        a, b = samples[0], samples[1]
        return max((abs(a.get(m, 0.0) - b.get(m, 0.0)) for m in a), default=0.0)
    half = max(1, len(samples) // 2)
    left, right = samples[:half], samples[half:] or samples[:half]
    worst = 0.0
    for m in left[0]:
        lv = median([s.get(m, 0.0) for s in left])
        rv = median([s.get(m, 0.0) for s in right])
        worst = max(worst, abs(lv - rv))
    return worst


# ── agent replay behaviour ─────────────────────────────────────────────────────

def probe_count(turns: int) -> int:
    return max(_MIN_PROBES, min(_MAX_PROBES, -(-int(turns or 0) * 100 // 400) or _MIN_PROBES))


async def measure_agent(
    ask: Any, prompts: list[str], *, judge: Any | None = None,
) -> tuple[str, float, list[str]]:
    """Classify how much the agent's answers move when the input is unchanged.

    `ask(prompt)` is awaited `_REPLIES` times per prompt. Identical text needs no
    judgment. Otherwise `judge(prompt, replies)` decides whether the difference is
    wording or behaviour; without a judge, differing text is treated as wording.
    """
    if not prompts:
        return STABLE, 1.0, []

    drifted: list[str] = []
    identical = True
    batches: list[tuple[str, list[str]]] = []
    for prompt in prompts:
        replies = [str(r or "") for r in await asyncio.gather(
            *[ask(prompt) for _ in range(_REPLIES)]
        )]
        if len({r.strip() for r in replies}) > 1:
            identical = False
            batches.append((prompt, replies))

    if identical:
        return DETERMINISTIC, 1.0, []
    if judge is None:
        return STABLE, 0.9, []

    equivalent = 0
    for prompt, replies in batches:
        verdict = await judge(prompt, replies)
        if verdict.get("equivalent"):
            equivalent += 1
        else:
            for d in verdict.get("drifted") or []:
                if d not in drifted:
                    drifted.append(str(d))
    ratio = equivalent / len(batches) if batches else 1.0
    unchanged = len(prompts) - len(batches)
    determinism = (unchanged + equivalent * 0.9) / len(prompts)
    return (STABLE if ratio == 1.0 else VOLATILE), determinism, drifted
