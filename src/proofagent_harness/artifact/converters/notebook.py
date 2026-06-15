"""Jupyter notebook (.ipynb) converter — stdlib JSON, no external deps.

Renders cells in order with markdown # prefixes so the chunker can split
on logical boundaries. Code cells are wrapped in fenced code blocks;
markdown cells are rendered as-is; outputs are summarized (not dumped
verbatim to keep prompt size sane).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ArtifactConversionError


def read_notebook(p: Path) -> str:
    try:
        nb = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to parse notebook {p}: {type(exc).__name__}: {exc}"
        ) from exc

    parts: list[str] = [f"# Jupyter notebook: {p.name}\n"]
    cells = nb.get("cells", [])
    for i, cell in enumerate(cells, start=1):
        ctype = cell.get("cell_type", "unknown")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        source = source.strip()
        if not source:
            continue

        if ctype == "markdown":
            parts.append(f"## Cell {i} (markdown)\n\n{source}\n")
        elif ctype == "code":
            # Language guess from notebook metadata.
            lang = (nb.get("metadata", {}).get("kernelspec", {}).get("language", "python"))
            parts.append(f"## Cell {i} (code · {lang})\n\n```{lang}\n{source}\n```\n")
            # Summarize outputs without dumping all of them.
            outputs = cell.get("outputs", [])
            if outputs:
                summary_bits: list[str] = []
                for out in outputs:
                    otype = out.get("output_type", "")
                    if otype == "stream":
                        text = "".join(out.get("text", []))
                        if text.strip():
                            summary_bits.append(f"  [stream: {text[:200]!r}{'...' if len(text) > 200 else ''}]")
                    elif otype in ("display_data", "execute_result"):
                        data = out.get("data", {})
                        if "text/plain" in data:
                            text = data["text/plain"]
                            if isinstance(text, list):
                                text = "".join(text)
                            summary_bits.append(f"  [output: {text[:200]!r}{'...' if len(text) > 200 else ''}]")
                        elif "image/png" in data:
                            summary_bits.append("  [output: <image>]")
                    elif otype == "error":
                        ename = out.get("ename", "Error")
                        evalue = out.get("evalue", "")
                        summary_bits.append(f"  [error: {ename}: {evalue}]")
                if summary_bits:
                    parts.append("\n".join(summary_bits) + "\n")
        else:
            parts.append(f"## Cell {i} ({ctype})\n\n{source}\n")

    return "\n".join(parts)
