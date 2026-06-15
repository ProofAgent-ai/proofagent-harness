"""PDF converter via pypdf (optional dep)."""

from __future__ import annotations

from pathlib import Path

from . import ArtifactConversionError


def read_pdf(p: Path) -> str:
    try:
        from pypdf import PdfReader                                  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArtifactConversionError(
            "PDF support requires pypdf. Install with one of:\n"
            "    pip install proofagent-harness[artifact]\n"
            "    pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(str(p))
        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                pages.append(f"--- Page {i + 1} ---\n{text}")
        if not pages:
            return f"[PDF text extraction returned empty — likely scanned/image-only: {p.name}]"
        return "\n\n".join(pages)
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to parse PDF {p}: {type(exc).__name__}: {exc}"
        ) from exc
