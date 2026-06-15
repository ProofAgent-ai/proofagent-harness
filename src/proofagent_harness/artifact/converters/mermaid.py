"""Mermaid / PlantUML diagram converter.

For `.mmd` / `.mermaid` files: surface the diagram source verbatim
inside a markdown code fence so the juror can read the graph structure
(nodes, edges, labels). Mermaid is text-encoded — the LLM can reason
about it directly without OCR.

For `.md` files containing mermaid code blocks (handled at the read_plain_text
level, since those are already markdown), no extra work is needed.
"""

from __future__ import annotations

from pathlib import Path

from . import ArtifactConversionError


def read_mermaid(p: Path) -> str:
    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ArtifactConversionError(
            f"Failed to read mermaid file {p}: {type(exc).__name__}: {exc}"
        ) from exc

    # Detect first-line directive (graph TD, flowchart LR, sequenceDiagram, …)
    # so the juror sees the diagram class upfront.
    first_line = body.strip().split("\n", 1)[0].strip() if body.strip() else ""
    diagram_type = "diagram"
    for kind in ("graph", "flowchart", "sequenceDiagram", "classDiagram",
                 "stateDiagram", "erDiagram", "gantt", "pie", "journey"):
        if first_line.startswith(kind):
            diagram_type = kind
            break

    return (
        f"# Mermaid diagram: {p.name}\n\n"
        f"**Diagram type:** `{diagram_type}` (text-encoded — node names and "
        f"edge labels are verifiable directly from the source below).\n\n"
        f"```mermaid\n{body}\n```\n"
    )
