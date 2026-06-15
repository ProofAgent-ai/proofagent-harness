"""File-format → text converters for AgentArtifact and KnowledgeCorpus.

v0.5.0 supports:
  * .md / .txt / .markdown / .rst    — plain text (always works, no deps)
  * code extensions                  — read as UTF-8 (.py, .ts, .js, .go, …)
  * .pdf                             — via `pypdf` (optional)
  * .docx                            — via `python-docx` (optional)
  * .html / .htm                     — via `beautifulsoup4` (optional)
  * .png / .jpg / .jpeg / .gif / .webp / .svg
                                     — via vision-capable LLM (uses the
                                       Harness LLM if supported)
  * .ipynb                           — stdlib json (no deps)
  * .json (structured-plan shape)    — pretty-printed as decision-list
  * .mmd / mermaid blocks in .md     — surfaced as text-encoded graph
  * .log / .jsonl                    — collapsed to compact summary

For optional extras, install:
    pip install proofagent-harness[artifact]
    # or piecemeal:
    pip install pypdf python-docx beautifulsoup4

The dispatcher in this module routes a Path to the right converter
based on its extension. Image conversion requires a `vision_llm`
parameter (typically `harness.llm`); without it, image files return a
placeholder description rather than raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Extensions we treat as plain text — read with UTF-8 (with replacement
# error handler so a stray byte doesn't blow up the whole eval).
PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".txt", ".markdown", ".rst",
    # Common code extensions
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".rb", ".php", ".cs", ".swift", ".m",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".graphql", ".proto",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".tf", ".tfvars",
    ".css", ".scss",
    ".xml", ".csv", ".tsv",
    # v0.5.0 — explicitly include log / structured-text formats so the
    # knowledge corpus walker doesn't silently skip them when a customer
    # passes a folder of agent execution traces.
    ".log",
})

PDF_EXTENSIONS:      frozenset[str] = frozenset({".pdf"})
DOCX_EXTENSIONS:     frozenset[str] = frozenset({".docx"})
HTML_EXTENSIONS:     frozenset[str] = frozenset({".html", ".htm"})
IMAGE_EXTENSIONS:    frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
NOTEBOOK_EXTENSIONS: frozenset[str] = frozenset({".ipynb"})
JSON_EXTENSIONS:     frozenset[str] = frozenset({".json"})
JSONL_EXTENSIONS:    frozenset[str] = frozenset({".jsonl", ".ndjson"})
MERMAID_EXTENSIONS:  frozenset[str] = frozenset({".mmd", ".mermaid"})


class ArtifactConversionError(Exception):
    """Raised when a file can't be converted to text (bad path, missing
    optional dep, unsupported extension)."""


def convert_to_text(
    path: Path | str,
    *,
    vision_llm: Any = None,
) -> str:
    """Load a file and return its text content, auto-converting the format.

    Dispatches on the file extension. Raises ArtifactConversionError with
    a self-explanatory message when:
      * the path doesn't exist or isn't a regular file
      * the extension isn't supported
      * an optional conversion dependency isn't installed

    For images: `vision_llm` should be a vision-capable LLM instance
    (typically the Harness's `self.llm`). If omitted, returns a
    placeholder description (warning) rather than raising — keeps the
    eval running on partial input.

    Always returns a non-empty string on success.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise ArtifactConversionError(f"Artifact path does not exist: {p}")
    if not p.is_file():
        raise ArtifactConversionError(
            f"Artifact path is not a regular file: {p} "
            f"(directories aren't supported for artifacts — pass them as a KnowledgeCorpus instead)"
        )

    ext = p.suffix.lower()

    # Plain text + code (covers ~70% of artifacts).
    if ext in PLAIN_TEXT_EXTENSIONS or ext == "":
        from .text import read_plain_text
        return read_plain_text(p)
    # Logs — pre-process for noise reduction.
    if ext == ".log":
        from .logs import read_log
        return read_log(p)
    # Notebooks — strip cells.
    if ext in NOTEBOOK_EXTENSIONS:
        from .notebook import read_notebook
        return read_notebook(p)
    # JSON-Lines — collapsed event stream.
    if ext in JSONL_EXTENSIONS:
        from .logs import read_jsonl
        return read_jsonl(p)
    # Structured JSON — render decision-list shape juror-friendly.
    if ext in JSON_EXTENSIONS:
        from .structured_json import read_structured_json
        return read_structured_json(p)
    # Mermaid / PlantUML source files — surface as text-encoded graph.
    if ext in MERMAID_EXTENSIONS:
        from .mermaid import read_mermaid
        return read_mermaid(p)
    # PDF / DOCX / HTML — optional deps.
    if ext in PDF_EXTENSIONS:
        from .pdf import read_pdf
        return read_pdf(p)
    if ext in DOCX_EXTENSIONS:
        from .docx import read_docx
        return read_docx(p)
    if ext in HTML_EXTENSIONS:
        from .html import read_html
        return read_html(p)
    # Images — vision LLM.
    if ext in IMAGE_EXTENSIONS:
        from .image import read_image
        return read_image(p, vision_llm=vision_llm)

    raise ArtifactConversionError(
        f"Unsupported artifact extension: {ext!r} (file: {p}).\n"
        f"v0.5.0 supports: .md, .txt, .pdf, .docx, .html, .png, .jpg, .ipynb, "
        f".json, .jsonl, .log, .mmd, plus most code extensions.\n"
        f"For unsupported formats, pre-extract the text yourself and pass it as "
        f"AgentArtifact(generated_artifact=text, type=...) directly."
    )


__all__ = [
    "ArtifactConversionError",
    "convert_to_text",
    "PLAIN_TEXT_EXTENSIONS",
    "PDF_EXTENSIONS",
    "DOCX_EXTENSIONS",
    "HTML_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "NOTEBOOK_EXTENSIONS",
    "JSON_EXTENSIONS",
    "JSONL_EXTENSIONS",
    "MERMAID_EXTENSIONS",
]
