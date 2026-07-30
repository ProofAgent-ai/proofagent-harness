"""Check vocabulary, sentinel resolution, and the behaviour/control join.

Loads the four data files that make scoring arithmetic instead of judgment:

    checks.yaml             the binary observations a metric score is counted from
    sentinels.yaml          planted-marker types that make a boundary decidable
    behaviours.yaml         framework-neutral vocabulary of what went wrong
    control_behaviours.yaml which regulatory control covers which behaviour

The join that replaces the compliance LLM pass:

    failed check -> check.probes -> behaviour -> every control covering it

Everything here is pure and cached. No LLM, no network, no clock, no randomness —
that is the point. `sentinel_value` in particular derives from a SHA-256 of
(seed, trap, type, slot): a `random` call there would silently undo the
reproducibility the whole mechanism exists to buy, and `hash()` would too since it
is salted per interpreter run.
"""

from __future__ import annotations

import hashlib
import string
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from proofagent_harness.schemas import CheckDef, SentinelDef, Trap

_DATA = Path(__file__).parent / "data"

# Directive prefixes a template may carry instead of a literal value. They tell the
# deterministic layer to read something already on the trap or agent rather than to
# plant a new marker, so no value is generated for them.
_DIRECTIVE = "@"


def _data(name: str) -> dict[str, Any]:
    with open(_DATA / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def load_checks() -> dict[str, CheckDef]:
    """All check definitions by id."""
    raw = _data("checks.yaml").get("checks") or {}
    return {cid: CheckDef(id=cid, **(body or {})) for cid, body in raw.items()}


@lru_cache(maxsize=1)
def load_sentinels() -> dict[str, SentinelDef]:
    """All sentinel type definitions by id."""
    raw = _data("sentinels.yaml").get("types") or {}
    return {sid: SentinelDef(id=sid, **(body or {})) for sid, body in raw.items()}


@lru_cache(maxsize=1)
def load_behaviours() -> dict[str, dict[str, Any]]:
    """Behaviour vocabulary by id."""
    return dict(_data("behaviours.yaml").get("behaviours") or {})


@lru_cache(maxsize=1)
def load_control_behaviours() -> dict[str, dict[str, list[str]]]:
    """framework -> control -> behaviours covered."""
    return dict(_data("control_behaviours.yaml").get("frameworks") or {})


@lru_cache(maxsize=1)
def _behaviour_index() -> dict[str, tuple[tuple[str, str], ...]]:
    """behaviour -> ((framework, control), ...). Inverted once, read many."""
    idx: dict[str, list[tuple[str, str]]] = {}
    for fw, controls in load_control_behaviours().items():
        for control, behaviours in (controls or {}).items():
            for b in behaviours or []:
                idx.setdefault(b, []).append((fw, control))
    return {b: tuple(v) for b, v in idx.items()}


def controls_for_behaviour(behaviour: str) -> tuple[tuple[str, str], ...]:
    """Every (framework, control) that treats this behaviour as evidence."""
    return _behaviour_index().get(behaviour, ())


def controls_for_behaviours(behaviours: set[str]) -> dict[str, set[str]]:
    """framework -> {control} implicated by any of these behaviours."""
    out: dict[str, set[str]] = {}
    for b in behaviours:
        for fw, control in controls_for_behaviour(b):
            out.setdefault(fw, set()).add(control)
    return out


# ── trap -> checks ───────────────────────────────────────────────────────────


def checks_for(trap: Trap | None) -> list[CheckDef]:
    """The trap's declared checks, in file order, silently dropping unknown ids.

    Unknown ids are dropped rather than raised because a report must still be
    produced when a user-supplied trap references a check this build does not have.
    `validate_trap_checks` is the place that turns them into visible errors.
    """
    if trap is None:
        return []
    vocab = load_checks()
    declared = list(getattr(trap, "checks", None) or [])
    return [vocab[cid] for cid in declared if cid in vocab]


def validate_trap_checks(trap: Trap) -> list[str]:
    """Human-readable problems with a trap's `checks:` list. Empty = clean."""
    vocab, sents = load_checks(), load_sentinels()
    problems: list[str] = []
    declared = list(getattr(trap, "checks", None) or [])

    for cid in declared:
        if cid not in vocab:
            problems.append(f"unknown check id {cid!r}")
            continue
        c = vocab[cid]
        if c.sentinel and c.sentinel not in sents:
            problems.append(f"check {cid!r} needs sentinel {c.sentinel!r}, which is undefined")
        if c.families and trap.family and trap.family not in c.families:
            # A hint, not an error: a trap may legitimately reach outside its family.
            problems.append(
                f"check {cid!r} is not tagged for family {trap.family!r} (hint only)"
            )

    if len(declared) != len(set(declared)):
        problems.append("duplicate check ids")

    if "paired_outcome_diverged" in declared and not getattr(trap, "paired", None):
        problems.append("declares paired_outcome_diverged but has no `paired:` block")

    if "called_forbidden_tool" in declared and not trap.forbidden_tools:
        problems.append("declares called_forbidden_tool but no `forbidden_tools:`")

    if "skipped_required_tool" in declared and not trap.expected_tools:
        problems.append("declares skipped_required_tool but no `expected_tools:`")

    return problems


def metrics_for(trap: Trap | None) -> set[str]:
    """Metrics the trap's checks can move."""
    return {m for c in checks_for(trap) for m in c.metrics}


def sentinel_types_for(trap: Trap | None) -> list[str]:
    """Sentinel types the trap needs planted, deduped, in first-use order."""
    seen: list[str] = []
    for c in checks_for(trap):
        if c.sentinel and c.sentinel not in seen:
            seen.append(c.sentinel)
    return seen


# ── sentinel resolution ──────────────────────────────────────────────────────

_SLOTS: dict[str, tuple[str, int]] = {
    # slot -> (alphabet kind, width). Narrow widths exist because several formats are
    # length-constrained — a card's final group is exactly 4 digits, an ICD subcode 2.
    # A wrong width is a tell that the value is synthetic, which is the one property a
    # sentinel must not have.
    "{n2}": ("digits", 2),
    "{n3}": ("digits", 3),
    "{n4}": ("digits", 4),
    "{n6}": ("digits", 6),
    "{a3}": ("upper", 3),
    "{a6}": ("upper", 6),
}


def _stream(seed: int, trap_name: str, type_id: str, slot: str) -> bytes:
    """Deterministic byte stream for one slot.

    SHA-256 over a UTF-8 key, so the same inputs give the same bytes on every
    machine, interpreter, and Python version. The slot is part of the key so two
    slots in one template do not collide onto the same digits.
    """
    key = f"{seed}|{trap_name}|{type_id}|{slot}".encode()
    return hashlib.sha256(key).digest()


def _fill(seed: int, trap_name: str, type_id: str, slot: str) -> str:
    kind, width = _SLOTS[slot]
    raw = _stream(seed, trap_name, type_id, slot)
    if kind == "digits":
        # Modulo over a wide integer, then zero-pad, so every width is uniform-ish
        # and stable. Leading zeros are kept: they are part of the literal to match.
        n = int.from_bytes(raw[:8], "big") % (10**width)
        return str(n).zfill(width)
    alphabet = string.ascii_uppercase
    return "".join(alphabet[b % len(alphabet)] for b in raw[:width])


def is_directive(template: str) -> bool:
    """True when a template points at existing trap/agent data instead of a value."""
    return template.startswith(_DIRECTIVE)


def sentinel_value(
    seed: int,
    trap_name: str,
    type_id: str,
    domain: str | None = None,
) -> str:
    """Resolve a sentinel type to its planted value for this run.

    Derived, never random: the same (seed, trap, type, domain) yields the same value
    on a different machine, which is what lets two people reach the same verdict
    without sharing anything. Directive templates (`@tools:...`, `@forbidden_tools`)
    are returned untouched for the deterministic layer to interpret.
    """
    sents = load_sentinels()
    sd = sents.get(type_id)
    if sd is None:
        return ""
    template = sd.template(domain)
    if not template or is_directive(template):
        return template
    out = template
    for slot in _SLOTS:
        if slot in out:
            out = out.replace(slot, _fill(seed, trap_name, type_id, slot))
    return out


def sentinels_for(
    trap: Trap | None,
    seed: int,
    domain: str | None = None,
) -> dict[str, str]:
    """type_id -> resolved value for every sentinel this trap needs."""
    if trap is None:
        return {}
    return {
        t: sentinel_value(seed, trap.name, t, domain)
        for t in sentinel_types_for(trap)
    }


def directive_targets(template: str) -> list[str]:
    """Parse a directive template into the names it points at.

    ``@tools:verify,authenticate`` -> ``["verify", "authenticate"]``
    ``@forbidden_tools``          -> ``["forbidden_tools"]``
    """
    if not is_directive(template):
        return []
    body = template[len(_DIRECTIVE):]
    if ":" in body:
        _, names = body.split(":", 1)
        return [n.strip() for n in names.split(",") if n.strip()]
    return [body.strip()]
