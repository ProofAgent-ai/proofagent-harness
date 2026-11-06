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
    out = []
    for t in rep.get("transcript") or []:
        name, turn = t.get("trap_name"), t.get("turn_index")
        if not name or not isinstance(turn, int):
            continue
        trap = library.get(name)
        family = getattr(trap, "family", None)
        pattern = str(getattr(trap, "pattern", "") or "").strip()
        row = {"turn": turn, "scenario": name, "scenario_label": _label(name),
                    "severity": (str(getattr(trap, "severity", "") or "").upper() or None),
                    "category": family, "category_label": _label(family) if family else None,
                    "technique": name, "technique_label": pattern.split(". ")[0][:200] if pattern else None,
                    "tags": list(getattr(trap, "tags", None) or []),
                    "user": str(t.get("question") or "")[:12000], "synthetic": True}
        if include_agent:
            row["agent"] = str(t.get("answer") or "")[:12000]
        out.append(row)
    return out


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
