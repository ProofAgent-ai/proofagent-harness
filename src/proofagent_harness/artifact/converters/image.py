"""Image / diagram converter via vision-capable LLM.

When an artifact is a PNG / JPG / SVG / etc. — typically an architecture
diagram, screenshot, or generated chart — we use the Harness's own LLM
(if vision-capable) to produce a structured text description the juror
can score against the corpus.

Why vision-LLM vs. OCR:
  * Diagrams are graphs, not text. OCR misses component relationships,
    arrow directions, and grouping boxes.
  * Vision LLMs trained 2024+ (gpt-4.1-mini, claude-haiku-4-5,
    gemini-2.0-flash) describe diagrams in semantically meaningful
    terms: "FastAPI gateway forwards POST /refund/review to a
    LangGraph supervisor; supervisor dispatches to 3 worker agents
    (DocVal, PolicyComp, FraudDetect) in parallel".
  * Reuses the Harness's existing LLM — no new dependency.

Graceful degradation:
  * If `vision_llm` is None → returns a placeholder so the eval still
    runs on partial input (image content unscored, rest of bundle still
    scored). Emits a warning the runner surfaces in the report.
  * If the LLM call fails (rate limit, vision-not-supported by model) →
    same placeholder + warning.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any


# Recognized image MIME types for the OpenAI / Anthropic vision payload.
_MIME_TYPES: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


# Prompt the vision LLM uses to describe the diagram. Tuned for auditor
# usefulness: structured, citation-friendly, no fluff.
_VISION_PROMPT = """You are describing a diagram for a software auditor who
cannot see images. Their job is to verify the diagram against a written
specification. Produce a structured description with:

  1. **Components** — list every named box / icon / labeled element,
     with its label verbatim.
  2. **Connections** — list every arrow / line / data flow as
     "<source> → <target> [label if any]".
  3. **Groupings** — any container boxes, swim lanes, or shaded
     regions and what they group.
  4. **Annotations** — any text labels, version stamps, notes.
  5. **Visual structure** — top-down vs left-right flow? Tree, cycle,
     star, mesh?

Be EXHAUSTIVE on names — the auditor will cross-check every component
against a list. Do NOT interpret or evaluate; just describe what is
visible. If text is unreadable, say so explicitly. Output in
markdown."""


def read_image(p: Path, *, vision_llm: Any = None) -> str:
    """Describe an image artifact using a vision-capable LLM.

    Returns the structured description text. Never raises on vision
    failure — falls back to a placeholder so the rest of the eval can
    proceed (the artifact runner surfaces the warning event).
    """
    if vision_llm is None:
        return _placeholder(p, reason="no vision LLM available (pass `vision_llm=harness.llm` or use a vision-capable Harness LLM)")

    ext = p.suffix.lower()

    # SVG is XML text — no vision call needed, just inline + label.
    if ext == ".svg":
        try:
            svg_text = p.read_text(encoding="utf-8", errors="replace")
            return f"[SVG diagram: {p.name}]\n\n```xml\n{svg_text[:50_000]}\n```"
        except Exception as exc:
            return _placeholder(p, reason=f"SVG read failed: {exc}")

    mime = _MIME_TYPES.get(ext)
    if mime is None:
        return _placeholder(p, reason=f"unsupported image extension: {ext}")

    try:
        img_bytes = p.read_bytes()
    except Exception as exc:
        return _placeholder(p, reason=f"read failed: {exc}")

    b64 = base64.standard_b64encode(img_bytes).decode("ascii")

    # Vision message — uses OpenAI's image_url shape which most providers
    # (OpenAI, Anthropic via LiteLLM, Gemini via LiteLLM) accept.
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            ],
        }
    ]

    try:
        # Call the LLM — supports both sync and async LLM wrappers.
        if hasattr(vision_llm, "acomplete"):
            response = _run_sync(vision_llm.acomplete(messages, max_tokens=2048, temperature=0))
        else:
            response = vision_llm.complete(messages, max_tokens=2048, temperature=0)
        text = (response if isinstance(response, str) else getattr(response, "text", "")) or ""
        if not text.strip():
            return _placeholder(p, reason="vision LLM returned empty response (likely not vision-capable)")
        return f"# Diagram description: {p.name}\n\n{text}"
    except Exception as exc:
        return _placeholder(p, reason=f"vision LLM call failed: {type(exc).__name__}: {exc}")


def _placeholder(p: Path, *, reason: str) -> str:
    """Best-effort placeholder when vision conversion isn't possible.

    Includes the file size so the juror knows there WAS content, just
    inaccessible — better than silently treating the artifact as empty.
    """
    try:
        size = p.stat().st_size
    except Exception:
        size = 0
    return (
        f"# Image artifact: {p.name}\n\n"
        f"**[image content not converted to text — {reason}]**\n\n"
        f"- file: {p}\n"
        f"- size: {size:,} bytes\n"
        f"- format: {p.suffix.lower()}\n\n"
        f"The juror should NOT score this artifact's visual content. If "
        f"the surrounding artifact bundle references this image (e.g. "
        f"\"see architecture diagram\"), evaluate only against textual "
        f"claims about it — not the image itself."
    )


def _run_sync(coro: Any) -> Any:
    """Run an async coroutine from a sync context, handling already-running loops."""
    try:
        asyncio.get_running_loop()
        # We're in an async context — schedule + wait via threading.
        import threading
        box: dict[str, Any] = {}

        def runner() -> None:
            try:
                box["result"] = asyncio.run(coro)
            except BaseException as exc:
                box["exc"] = exc

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join()
        if "exc" in box:
            raise box["exc"]
        return box["result"]
    except RuntimeError:
        return asyncio.run(coro)
