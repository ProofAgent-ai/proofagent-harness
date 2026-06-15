"""HTML converter via BeautifulSoup (optional dep). Drops noise tags,
converts heading tags to markdown # prefixes so the chunker can later
split on them."""

from __future__ import annotations

from pathlib import Path

from . import ArtifactConversionError


def read_html(p: Path) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArtifactConversionError(
            "HTML support requires beautifulsoup4. Install with one of:\n"
            "    pip install proofagent-harness[artifact]\n"
            "    pip install beautifulsoup4"
        ) from exc

    try:
        soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="replace"), "html.parser")
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to parse HTML {p}: {type(exc).__name__}: {exc}"
        ) from exc

    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            text = h.get_text(" ", strip=True)
            h.replace_with(f"\n\n{'#' * level} {text}\n\n")

    text = soup.get_text("\n", strip=True)
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        if line:
            cleaned.append(line)
            blank_run = 0
        else:
            blank_run += 1
            if blank_run < 2:
                cleaned.append("")
    return "\n".join(cleaned) if cleaned else f"[HTML appeared empty: {p.name}]"
