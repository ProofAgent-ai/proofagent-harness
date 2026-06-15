"""Semantic chunking for large artifacts.

In v0.5.0 the artifact runner uses a single jury pass over the full
artifact when it fits the LLM context window. This module is the
forward-compatible foundation: it can split an artifact on semantic
boundaries (markdown headings) or via a sliding character window with
overlap, ready for v0.5.1's multi-chunk parallel scoring.

The runner currently uses `should_chunk()` to decide whether to chunk +
warn, and falls back to in-prompt truncation when the artifact fits.

Public API
----------
    chunk_artifact(text, policy) -> list[Chunk]
    should_chunk(text, policy)   -> bool
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from proofagent_harness.schemas import ChunkingPolicy

# Match markdown ATX headings: #, ##, ### up to ######. Captures the line
# AND the level (# = 1, ## = 2, etc.) so the hierarchical chunker can
# preserve parent-section context when splitting children.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PARENT_PREFIX_TEMPLATE = "\n[Parent section context: {parent_chain}]\n\n"

# Fallback paragraph break — two-or-more newlines.
_PARAGRAPH_RE = re.compile(r"\n{2,}")


@dataclass(frozen=True)
class Chunk:
    """One slice of a chunked artifact."""

    index: int
    """Zero-based position in the chunk sequence."""

    text: str
    """The chunk's text content (with any overlap prefix from chunk N-1)."""

    heading: str
    """The closest H1/H2/H3 heading that started this chunk, or '' if none."""

    char_start: int
    """Character offset in the original artifact where this chunk begins
    (BEFORE applying the overlap prefix). Used for telemetry."""

    char_end: int
    """Character offset where this chunk ends. char_end - char_start is the
    chunk's net contribution to the artifact (excluding overlap)."""


def should_chunk(text: str, policy: ChunkingPolicy) -> bool:
    """Return True if the artifact exceeds the per-chunk budget."""
    return len(text) > policy.max_chunk_chars


def chunk_artifact(text: str, policy: ChunkingPolicy) -> list[Chunk]:
    """Split `text` into chunks per `policy`.

    Strategy:
      1. If `text` fits in `policy.max_chunk_chars`, return a single chunk
         (zero-overhead path).
      2. Otherwise, if `policy.prefer_semantic`, try heading-based splits:
         walk H1/H2/H3 boundaries and group consecutive sections until the
         budget is full, then start a new chunk.
      3. Sliding-window fallback (always available): cut at the budget,
         preferring the last sentence boundary within the last ~10% of the
         budget. Prefix `overlap_chars` from the previous chunk's tail onto
         the next chunk for cross-boundary context.

    Returns a list of Chunk, length ≥ 1. Each chunk's `text` includes its
    overlap prefix; `char_start`/`char_end` reflect the original artifact
    positions BEFORE overlap so callers can reconstruct contributions.
    """
    if len(text) <= policy.max_chunk_chars:
        # Fast path — no chunking needed.
        return [Chunk(index=0, text=text, heading="", char_start=0, char_end=len(text))]

    if policy.prefer_semantic and _HEADING_RE.search(text):
        chunks = _semantic_chunk(text, policy)
        if chunks:
            return _apply_overlap(chunks, policy.overlap_chars)

    # Sliding-window fallback.
    return _apply_overlap(_window_chunk(text, policy), policy.overlap_chars)


# ─── Semantic chunking ───────────────────────────────────────────────────────

def _semantic_chunk(text: str, policy: ChunkingPolicy) -> list[Chunk]:
    """Group H1/H2/H3 sections until the budget is full, then start a new chunk.

    Hierarchy-aware (v0.5.0): tracks the H1 / H2 parent chain. When a
    chunk starts at an H3, it is prefixed with `[Parent section context:
    H1 > H2]` so the juror doesn't lose the outer-section meaning.

    Returns [] (caller falls back to window chunking) when:
      * the largest single heading section exceeds the budget on its own
      * heading detection produces only one section
    """
    # Locate each heading line: (start_offset, heading_text, level).
    heads: list[tuple[int, str, int]] = []
    for m in _HEADING_RE.finditer(text):
        level = len(m.group(1))
        heads.append((m.start(), m.group(2).strip(), level))

    if len(heads) < 2:
        return []

    # Build sections: (start, end, heading, level).
    sections: list[tuple[int, int, str, int]] = []
    for i, (start, heading, level) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        sections.append((start, end, heading, level))
        if end - start > policy.max_chunk_chars:
            return []

    # Track the H1 / H2 chain so chunks starting at H3 (or deeper) get a
    # parent-context prefix. This preserves nested-section coherence:
    # the juror reading "### F — Functional Requirements" inside a chunk
    # still knows that its parent is "## 2. FOCUSED Analysis".
    parent_chain: list[str] = []   # heading texts at H1 + H2 levels

    chunks: list[Chunk] = []
    cur_start = sections[0][0]
    cur_heading = sections[0][2]
    cur_end = cur_start
    cur_parent_prefix = ""
    for start, end, heading, level in sections:
        # Update parent chain BEFORE deciding whether to flush.
        if level <= 2:
            parent_chain = parent_chain[: level - 1] + [heading]

        sec_len = end - start
        if cur_end != cur_start and (cur_end - cur_start + sec_len) > policy.max_chunk_chars:
            # Flush current chunk; the next chunk gets parent context.
            chunks.append(Chunk(
                index=len(chunks),
                text=cur_parent_prefix + text[cur_start:cur_end],
                heading=cur_heading,
                char_start=cur_start,
                char_end=cur_end,
            ))
            cur_start = start
            cur_heading = heading
            # Build the parent-context prefix for the next chunk if we're
            # starting at H3+ (children need the H1/H2 context).
            if level >= 3 and parent_chain:
                cur_parent_prefix = _PARENT_PREFIX_TEMPLATE.format(
                    parent_chain=" > ".join(parent_chain),
                )
            else:
                cur_parent_prefix = ""
        cur_end = end

    if cur_end > cur_start:
        chunks.append(Chunk(
            index=len(chunks),
            text=cur_parent_prefix + text[cur_start:cur_end],
            heading=cur_heading,
            char_start=cur_start,
            char_end=cur_end,
        ))

    return chunks


# ─── Window (character) chunking ─────────────────────────────────────────────

def _window_chunk(text: str, policy: ChunkingPolicy) -> list[Chunk]:
    """Slide a character window across `text`, preferring sentence boundaries.

    The window slides by `max_chunk_chars - overlap_chars` per step, BUT we
    rewind to the last sentence boundary inside the last 10% of the window
    so chunks don't end mid-sentence. Always produces ≥ 1 chunk.
    """
    chunks: list[Chunk] = []
    n = len(text)
    pos = 0
    while pos < n:
        # Tentative window end.
        end = min(n, pos + policy.max_chunk_chars)

        # If we're not at the very end of the artifact, try to rewind to a
        # sentence boundary inside the last 10% of the window so we don't
        # cut a sentence in half.
        if end < n:
            tail_zone_start = max(pos + 1, end - max(200, policy.max_chunk_chars // 10))
            tail = text[tail_zone_start:end]
            # Look for the last paragraph break first, then sentence end.
            para_idx = tail.rfind("\n\n")
            if para_idx != -1:
                end = tail_zone_start + para_idx
            else:
                # Find the last period/!/? followed by whitespace.
                m = list(re.finditer(r"[\.\!\?]\s", tail))
                if m:
                    end = tail_zone_start + m[-1].end()

        chunk_text = text[pos:end]
        chunks.append(Chunk(
            index=len(chunks),
            text=chunk_text,
            heading="",
            char_start=pos,
            char_end=end,
        ))
        if end >= n:
            break
        pos = end  # Overlap is applied later by _apply_overlap.

    return chunks


# ─── Overlap application (shared) ────────────────────────────────────────────

def _apply_overlap(chunks: list[Chunk], overlap_chars: int) -> list[Chunk]:
    """Prefix `overlap_chars` from chunk N-1's tail onto chunk N's text.

    Preserves char_start / char_end (those refer to original positions) but
    rewrites `text` to include the overlap. Chunk 0 is unchanged.
    """
    if overlap_chars <= 0 or len(chunks) <= 1:
        return list(chunks)

    out: list[Chunk] = [chunks[0]]
    prev_tail = chunks[0].text[-overlap_chars:] if len(chunks[0].text) > overlap_chars else chunks[0].text
    for ch in chunks[1:]:
        overlapped = Chunk(
            index=ch.index,
            text=(
                f"[...continued from previous chunk...]\n{prev_tail}\n"
                f"[...new content begins below...]\n{ch.text}"
            ),
            heading=ch.heading,
            char_start=ch.char_start,
            char_end=ch.char_end,
        )
        out.append(overlapped)
        prev_tail = ch.text[-overlap_chars:] if len(ch.text) > overlap_chars else ch.text
    return out
