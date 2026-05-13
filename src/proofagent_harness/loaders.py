"""Loaders for skills, personas, traps, and knowledge.

All four use the same convention: markdown file with YAML frontmatter. One
loader per primitive type, each producing a strongly-typed Pydantic model.

The loaders auto-discover bundled assets under `proofagent_harness/data/` and
merge them with anything the user provides.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import frontmatter

from proofagent_harness.schemas import Persona, Skill, Trap

# ─────────────────────────────────────────────────────────────────────────────
# Bundled data location
# ─────────────────────────────────────────────────────────────────────────────


def _builtin_root() -> Path:
    """Filesystem path to the bundled `data/` directory inside the package."""
    with resources.as_file(resources.files("proofagent_harness").joinpath("data")) as p:
        return Path(p)


# ─────────────────────────────────────────────────────────────────────────────
# Skills
# ─────────────────────────────────────────────────────────────────────────────


def load_skills(extra_dirs: list[str] | None = None) -> list[Skill]:
    """Load all bundled skills, merged with any user-provided skill dirs."""
    skills: dict[str, Skill] = {}
    for path in _walk_md(_builtin_root() / "skills"):
        s = _parse_skill(path)
        skills[s.name] = s
    for d in extra_dirs or []:
        for path in _walk_md(Path(d)):
            s = _parse_skill(path)
            skills[s.name] = s  # later wins (user override)
    return list(skills.values())


def get_skill(skills: list[Skill], name: str) -> Skill | None:
    """Find a skill by name."""
    for s in skills:
        if s.name == name:
            return s
    return None


def get_skills_for(skills: list[Skill], *, type: str, metric: str | None = None) -> list[Skill]:
    """Filter skills by type and (optional) metric."""
    out = [s for s in skills if s.type == type]
    if metric:
        out = [s for s in out if s.metric is None or s.metric == metric]
    return out


def _parse_skill(path: Path) -> Skill:
    post = frontmatter.load(path)
    meta = post.metadata or {}
    return Skill(
        name=str(meta.get("name") or path.stem),
        type=str(meta.get("type") or "scoring"),  # type: ignore[arg-type]
        applies_to=list(meta.get("applies_to") or []),
        metric=meta.get("metric"),
        rubric_version=str(meta.get("rubric_version") or "1.0"),
        loads_tools=list(meta.get("loads_tools") or []),
        body=post.content.strip(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Personas
# ─────────────────────────────────────────────────────────────────────────────


def load_personas(names: list[str] | None = None) -> list[Persona]:
    """Load personas by name or path.

    Names matching `<builtin>/personas/<name>.md` resolve from the bundled
    library. Paths ending in `.md` are loaded directly.
    """
    if not names:
        names = ["rigorous", "lenient", "contrarian"]

    out: list[Persona] = []
    for n in names:
        p = Path(n)
        if p.suffix == ".md" and p.exists():
            out.append(_parse_persona(p))
        else:
            candidate = _builtin_root() / "personas" / f"{n}.md"
            if not candidate.exists():
                raise FileNotFoundError(f"Persona not found: {n}")
            out.append(_parse_persona(candidate))
    return out


def _parse_persona(path: Path) -> Persona:
    post = frontmatter.load(path)
    meta = post.metadata or {}
    return Persona(
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        body=post.content.strip(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Traps
# ─────────────────────────────────────────────────────────────────────────────


def load_traps(
    extra_dirs: list[str] | None = None,
    trap_packs: list[str] | None = None,
) -> list[Trap]:
    """Load all bundled traps + user-provided dirs + named trap packs."""
    traps: dict[str, Trap] = {}

    for path in _walk_md(_builtin_root() / "traps"):
        t = _parse_trap(path)
        traps[t.name] = t

    for d in extra_dirs or []:
        for path in _walk_md(Path(d)):
            t = _parse_trap(path)
            traps[t.name] = t

    for pack in trap_packs or []:
        # Trap packs are installed as `proofagent-traps-<pack>` packages.
        # When the package is importable, its `traps/` dir is discoverable.
        try:
            pkg = f"proofagent_traps_{pack}"
            with resources.as_file(resources.files(pkg).joinpath("traps")) as p:
                for path in _walk_md(Path(p)):
                    t = _parse_trap(path)
                    traps[t.name] = t
        except (ModuleNotFoundError, FileNotFoundError):
            # Pack not installed — ignore quietly; users see a runtime warning.
            pass

    return list(traps.values())


# ─────────────────────────────────────────────────────────────────────────────
# Inverted indices over the trap library — built once, used many times
# ─────────────────────────────────────────────────────────────────────────────


class TrapIndex:
    """Pre-built lookup tables over a list of traps. Built once at Harness init.

    Cheap O(1) access by:
        - domain name        → list[Trap]
        - metric name        → list[Trap]
        - family name        → list[Trap]
        - severity bucket    → list[Trap]
        - the universal pool → list[Trap]
    """

    __slots__ = (
        "all",
        "by_domain",
        "by_family",
        "by_metric",
        "by_name",
        "by_severity",
        "domain_specific",
        "universals",
    )

    def __init__(self, traps: list[Trap]) -> None:
        self.all: list[Trap] = list(traps)
        self.by_name: dict[str, Trap] = {t.name: t for t in traps}
        self.universals: list[Trap] = [t for t in traps if t.universal]
        self.domain_specific: list[Trap] = [t for t in traps if t.domains and not t.universal]

        self.by_domain: dict[str, list[Trap]] = {}
        for t in traps:
            for d in t.domains:
                self.by_domain.setdefault(d, []).append(t)

        self.by_metric: dict[str, list[Trap]] = {}
        for t in traps:
            for m in (t.metrics or ["__any__"]):
                self.by_metric.setdefault(m, []).append(t)

        self.by_family: dict[str, list[Trap]] = {}
        for t in traps:
            self.by_family.setdefault(t.family, []).append(t)

        self.by_severity: dict[str, list[Trap]] = {}
        for t in traps:
            self.by_severity.setdefault(t.severity, []).append(t)

    # ── ergonomic queries ──────────────────────────────────────────────

    def for_domains(self, domains: list[str] | set[str]) -> list[Trap]:
        """Return traps that match ANY of the given domains (deduped)."""
        out: dict[str, Trap] = {}
        for d in domains:
            for t in self.by_domain.get(d, []):
                out[t.name] = t
        return list(out.values())

    def relevant_pool(self, domains: list[str] | set[str]) -> list[Trap]:
        """Universals + traps matching the given domains (no domain-mismatch traps)."""
        out: dict[str, Trap] = {t.name: t for t in self.universals}
        for d in domains:
            for t in self.by_domain.get(d, []):
                out[t.name] = t
        return list(out.values())

    def stats(self) -> dict[str, int]:
        """Quick summary for `proof traps stats` and tests."""
        return {
            "total": len(self.all),
            "universal": len(self.universals),
            "domain_specific": len(self.domain_specific),
            "domains": len(self.by_domain),
            "metrics": len(self.by_metric),
            "families": len(self.by_family),
        }


def load_trap_index(
    extra_dirs: list[str] | None = None,
    trap_packs: list[str] | None = None,
) -> TrapIndex:
    """Convenience: load all traps and immediately build a TrapIndex."""
    return TrapIndex(load_traps(extra_dirs, trap_packs))


def select_traps(
    traps: list[Trap],
    *,
    metric: str | None = None,
    family: str | None = None,
    severity: list[str] | None = None,
    n: int | None = None,
) -> list[Trap]:
    """Filter and sample traps for a plan."""
    out = traps
    if metric:
        out = [t for t in out if metric in t.metrics or not t.metrics]
    if family:
        out = [t for t in out if t.family == family]
    if severity:
        out = [t for t in out if t.severity in severity]
    if n is not None:
        out = out[:n]
    return out


def _parse_trap(path: Path) -> Trap:
    post = frontmatter.load(path)
    meta = post.metadata or {}
    body = post.content.strip()

    # Allow seeds to live either in frontmatter or in a "## Seed examples" section
    seeds = list(meta.get("seeds") or [])
    if not seeds:
        seeds = _extract_seeds_from_body(body)

    return Trap(
        name=str(meta.get("name") or path.stem),
        family=str(meta.get("family") or path.parent.name),
        severity=str(meta.get("severity") or "medium"),  # type: ignore[arg-type]
        metrics=list(meta.get("metrics") or []),
        seeds=seeds,
        pattern=str(meta.get("pattern") or _extract_section(body, "Pattern")),
        pass_criteria=str(meta.get("pass_criteria") or _extract_section(body, "Pass criteria")),
        fail_criteria=meta.get("fail_criteria") or _extract_section(body, "Fail criteria"),
        expected_tools=list(meta.get("expected_tools") or []),
        forbidden_tools=list(meta.get("forbidden_tools") or []),
        tags=list(meta.get("tags") or []),
        domains=list(meta.get("domains") or []),
        universal=bool(meta.get("universal") or False),
    )


def _extract_seeds_from_body(body: str) -> list[str]:
    section = _extract_section(body, "Seed examples")
    if not section:
        return []
    seeds: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            seeds.append(line[2:].strip().strip('"'))
    return seeds


def _extract_section(body: str, header: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.lower().startswith(f"# {header.lower()}") or line.lower().startswith(
            f"## {header.lower()}"
        ):
            inside = True
            continue
        if inside and (line.startswith("# ") or line.startswith("## ")):
            break
        if inside:
            out.append(line)
    return "\n".join(out).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge corpus
# ─────────────────────────────────────────────────────────────────────────────


def load_knowledge(source: Any) -> str:
    """Normalize any `knowledge` input into a single concatenated text blob.

    Accepts:
        - a path string to a file (any extension; treated as text)
        - a path string to a directory (all .md/.txt files recursively)
        - a list of paths
        - a dict {label: text}
        - inline text (used as-is)
    """
    if source is None:
        return ""

    if isinstance(source, dict):
        return "\n\n".join(f"## {k}\n{v}" for k, v in source.items())

    if isinstance(source, list):
        return "\n\n".join(load_knowledge(s) for s in source)

    if isinstance(source, str):
        # Only attempt path resolution for strings short enough to be a real
        # filesystem path. Long strings (>= 1KB or containing newlines) are
        # always treated as inline text — avoids OSError on giant inputs.
        if len(source) < 1024 and "\n" not in source:
            try:
                p = Path(source)
                if p.is_file():
                    return p.read_text(errors="ignore")
                if p.is_dir():
                    chunks: list[str] = []
                    for f in sorted(p.rglob("*")):
                        if f.suffix.lower() in {".md", ".txt", ".rst"} and f.is_file():
                            chunks.append(
                                f"## {f.name}\n{f.read_text(errors='ignore')}"
                            )
                    return "\n\n".join(chunks)
            except (OSError, ValueError):
                pass
        # Not a path (or too long to be one) — treat as inline text
        return source

    return str(source)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _walk_md(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())
