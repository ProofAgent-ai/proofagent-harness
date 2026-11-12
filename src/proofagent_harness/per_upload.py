"""`proof run --upload`: send the run's PER 2.1 record to the platform, which stores and displays it.

The record is the one `--per` writes (``per_export.export_per``). It carries identifying values only as
sha256 fingerprints, so the upload also sends a *lookup*: the text behind each fingerprint, taken from the
exporter's bundle. The platform accepts a lookup row only when fingerprint == sha256(text) and the record
holds that fingerprint, so a lookup can name what the record already contains and nothing else.

Text from a confidential source stays on this machine: a bundle string under an object whose EIO
``data_class`` is anything but public/internal (or carries a confidential marker) is not sent. Its
fingerprint is still in the record; only the text is held back.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

RECORDS_PATH = "/api/v1/records"
_SHAREABLE = frozenset({"eio.data.public", "eio.data.internal"})
_CONFIDENTIAL_MARKERS = ("confidential", "restricted", "secret")


class PerUploadError(RuntimeError):
    """The platform refused the record, or could not be reached. ``status`` is the HTTP status (0: no reply)."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


def _confidential(data_class: Any) -> bool:
    if not isinstance(data_class, str):
        return False
    if data_class.startswith("eio.data."):
        return data_class not in _SHAREABLE
    return any(m in data_class.lower() for m in _CONFIDENTIAL_MARKERS)


def _shareable_strings(node: Any, held: bool = False):
    """Every string in the bundle, except those under a confidential data class."""
    if isinstance(node, dict):
        held = held or _confidential(node.get("data_class"))
        for v in node.values():
            yield from _shareable_strings(v, held)
    elif isinstance(node, list):
        for v in node:
            yield from _shareable_strings(v, held)
    elif isinstance(node, str) and not held:
        yield node


def build_lookup(record: dict, bundle: dict) -> list[dict]:
    """One {fingerprint, text} row per shareable bundle string whose fingerprint the record holds."""
    body = json.dumps(record, ensure_ascii=False)
    rows, seen = [], set()
    for text in _shareable_strings(bundle):
        fp = "sha256-" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        if fp not in seen and f'"{fp}"' in body:
            seen.add(fp)
            rows.append({"fingerprint": fp, "text": text})
    return rows


def _label(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").strip().capitalize()


def scenarios_from_report(report: Any, *, include_agent: bool = False) -> list[dict]:
    """One attack scenario per turn, for the platform to label the evidence ("turn 4 · fake expert quotes"): the
    trap the turn ran, its library metadata, and the attacker's message unmasked (`user`). The record masks
    personal data in every quote it keeps; the attacker's message holds only the harness's own synthetic data, so
    it is safe to show. The agent's reply (`agent`) is added only on opt-in (`--upload-transcript`): it can carry
    real data the agent read from its tools. Sent next to the record (POST /records `scenarios`), not in it."""
    from proofagent_harness.loaders import load_traps
    from proofagent_harness.per_export import _load

    rep = _load(report)
    try:
        library = {t.name: t for t in load_traps()}
    except Exception:  # a broken trap pack must never block the upload
        library = {}
    custom = (((rep.get("metadata") or {}).get("trap_selection") or {}).get("custom_meta") or {})
    # fixed / still_open / new per turn, from the re-test ledger (exploration runs only)
    reg = (rep.get("metadata") or {}).get("regression") or {}
    status_of = {int(r.get("turn")): k for k in ("fixed", "still_open", "new") for r in (reg.get(k) or [])
                 if isinstance(r, dict) and r.get("turn") is not None}
    out = []
    for t in rep.get("transcript") or []:
        name, turn = t.get("trap_name"), t.get("turn_index")
        if not name or not isinstance(turn, int):
            continue
        trap = library.get(name)
        family = getattr(trap, "family", None) or (custom.get(name) or {}).get("family")
        pattern = str(getattr(trap, "pattern", "") or "").strip()
        row = {"turn": turn, "scenario": name, "scenario_label": _label(name),
                    "severity": (str(getattr(trap, "severity", "") or (custom.get(name) or {}).get("severity")
                                     or "").upper() or None),
                    "category": family, "category_label": _label(family) if family else None,
                    "technique": name, "technique_label": pattern.split(". ")[0][:200] if pattern else None,
                    "tags": list(getattr(trap, "tags", None) or []),
                    "user": str(t.get("question") or "")[:12000], "synthetic": True}
        if turn in status_of:
            row["retest_status"] = status_of[turn]
        if include_agent:
            row["agent"] = str(t.get("answer") or "")[:12000]
        out.append(row)
    # Nothing identifying leaves the machine in clear: the attacker's message carries the
    # harness's synthetic case files (names, SSNs, cards, health conditions) and the agent's
    # reply can carry real data it read from its tools. Masked before upload — through
    # ProofAgent Nano when configured (typed placeholders, health data included), else the
    # EIO redactor (email, national id, card, IBAN, secrets).
    mask = _masker()
    for row in out:
        for k in ("user", "agent"):
            if row.get(k):
                row[k] = mask(row[k])
        row["masked_by"] = getattr(mask, "label", "eio-redaction")
    _attach_references(rep, out, mask)
    return out


_REF_WINDOW = 220


def _window(text: str, needle: str, width: int = _REF_WINDOW) -> str:
    """The passage around `needle` in `text`, with ellipses where it was cut."""
    i = text.find(needle)
    if i < 0:
        return ""
    lo, hi = max(0, i - width), min(len(text), i + len(needle) + width)
    return ("…" if lo else "") + text[lo:hi].strip() + ("…" if hi < len(text) else "")


def references_from_report(report: Any) -> list[dict]:
    """Every planted marker the agent repeated, with its proof: where the harness planted it (the turn and the
    untrusted content that carried it) and where the agent echoed it back. A marker such as `ref VAQ-6496` is
    unreadable alone — it is a seeded token the harness hid inside injected content, and its reappearance in the
    agent's reply is what a code check (e.g. `obeyed_injected_instruction`) proved. Built from the code-decided
    verdicts the PER already validated; the record itself is not changed."""
    from proofagent_harness.per_export import _load

    rep = _load(report)
    turns = [t for t in rep.get("transcript") or [] if isinstance(t.get("turn_index"), int)]
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for v in rep.get("check_verdicts") or []:
        marker = str(v.get("quote") or "").strip()
        if not (v.get("observed") is True and v.get("decided_by") == "code" and marker):
            continue
        key = (marker, int(v.get("turn_index") or 0))
        if key in seen:
            continue
        seen.add(key)
        planted = next((t for t in turns if marker in str(t.get("question") or "")), None)
        echoed = [t for t in turns if marker in str(t.get("answer") or "")]
        out.append({
            "marker": marker,
            "check_id": v.get("check_id"),
            "turn": key[1],
            "planted_turn": planted.get("turn_index") if planted else None,
            "planted_trap": planted.get("trap_name") if planted else None,
            "planted_in": _window(str(planted.get("question") or ""), marker) if planted else "",
            "echoed": [{"turn": t["turn_index"], "trap": t.get("trap_name"),
                        "excerpt": _window(str(t.get("answer") or ""), marker)} for t in echoed],
        })
    return out


def _attach_references(rep: dict, rows: list[dict], mask: Any) -> None:
    """Attach each reference to the scenario of the turn whose verdict it proves (POST /records `scenarios`), with
    every excerpt masked like the rest of the scenario text. The marker itself is the harness's own synthetic token;
    it is kept verbatim so the dashboard can find it in the quote and make it clickable."""
    by_turn = {r["turn"]: r for r in rows}
    for ref in references_from_report(rep):
        row = by_turn.get(ref["turn"])
        if row is None:
            continue

        def m(text: str) -> str:
            if not text:
                return text
            parts = text.split(ref["marker"])
            return ref["marker"].join(mask(p) if p.strip() else p for p in parts)

        ref["planted_in"] = m(ref["planted_in"])
        for e in ref["echoed"]:
            e["excerpt"] = m(e["excerpt"])
        row.setdefault("references", []).append(ref)


def upload_record(record: dict, lookup: list[dict], *, api_url: str, api_key: str,
                  harness_version: str, timeout: float = 120.0, scenarios: list[dict] | None = None) -> dict:
    """POST the record and its lookup; return the platform's reply (record_id, run_id, url, decision, …)."""
    url = api_url.rstrip("/") + RECORDS_PATH
    try:
        resp = httpx.post(
            url,
            json={"per": record, "lookup": lookup, "harness_version": harness_version, "scenarios": scenarios or []},
            headers={"Authorization": f"Bearer {api_key}", "X-ProofAgent-Harness-Version": harness_version},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise PerUploadError(f"could not reach {url}: {exc}") from exc
    if resp.status_code >= 300:
        raise PerUploadError(f"{url} returned {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
    return resp.json()


def mask_identifiers(text: str) -> str:
    """Deterministic, regex-only masking of identifying values: EIO's redactor (email, national
    id, secrets, Luhn-valid cards) plus any card-shaped digit run and spaced IBANs, which EIO
    lets through. Pure substitution, so a quote that was a substring of an answer stays one
    after both are masked. Used for every text the PER cites and every scenario uploaded."""
    import re as _re

    from eio_agents.evidence.redaction import redact

    out = redact(text or "")
    out = out[0] if isinstance(out, tuple) else out
    out = _re.sub(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b", "[REDACTED:national_id]", out)
    return _re.sub(r"\b(?:\d[ -]?){12,18}\d\b", "[REDACTED:card]", out)


def _masker():
    """Text -> masked text. Nano (PROOFAGENT_NANO_URL + PROOFAGENT_NANO_KEY) first; the EIO
    regex redactor when Nano is not configured or a call fails, so masking never silently
    turns off."""
    import os

    from eio_agents.evidence.redaction import redact

    # One ProofAgent key: a key from the platform's API keys page (pa_nano_…) also works for Nano, so with
    # PROOFAGENT_NANO_URL set, PROOFAGENT_API_KEY is enough (no second key). Masking through Nano is still
    # opt-in through the URL, because every masked text is a guardrail call on the workspace's allowance.
    # An older pa_live_ key is evaluation-only: Nano would refuse it, so it is never sent there.
    api_key = os.environ.get("PROOFAGENT_API_KEY") or ""
    key = os.environ.get("PROOFAGENT_NANO_KEY") or (api_key if api_key.startswith("pa_nano_") else "")
    url = (os.environ.get("PROOFAGENT_NANO_URL") or "").rstrip("/")

    import re as _re
    # Stricter than EIO's redactor on purpose: it masks a card only when it passes the Luhn
    # check and an IBAN only without spaces, so a synthetic card or "GB82 WEST 1234 ..." went
    # through in clear.
    card = _re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
    iban = _re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")

    def eio(text: str) -> str:
        return mask_identifiers(text)

    if not (url and key):
        eio.label = "eio-redaction"  # type: ignore[attr-defined]
        return eio

    client = httpx.Client(timeout=30.0)

    def nano(text: str) -> str:
        try:
            r = client.post(f"{url}/v1/scan", json={"text": text, "use_case": "agent_output"},
                            headers={"Authorization": f"Bearer {key}"})
            if r.status_code < 300:
                masked = ((r.json() or {}).get("text") or {}).get("masked")
                if isinstance(masked, str):
                    return eio(masked)   # belt and braces: regex pass over Nano's output
        except (httpx.HTTPError, ValueError):
            pass
        return eio(text)

    nano.label = "proofagent-nano+eio-redaction"  # type: ignore[attr-defined]
    return nano
