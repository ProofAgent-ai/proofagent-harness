"""Knowledge corpus loader — walk folders/files into a single grounded
ground-truth string for the artifact-mode juror prompts.

Walks the user's `KnowledgeCorpus.sources` list (each entry can be a file
or a directory, recursive). Concatenates the resulting text with clear
`--- {relative_path} ---` separators so the juror can cite a source by
filename when scoring `hallucination_resistance` or `instruction_following`.

Enforces `corpus.max_chars` as a hard budget — once hit, stops loading
and emits a `corpus_truncated` warning event so the user knows their
corpus didn't fully reach the jury.

v0.5.0 supports `.md` and `.txt` only (covers the common case: internal
docs, briefs, requirements). PDF/DOCX corpus support arrives in v0.5.1
via the same converters module the artifact loader uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from proofagent_harness.schemas import KnowledgeCorpus


@dataclass(frozen=True)
class LoadedCorpus:
    """The result of loading a KnowledgeCorpus from disk."""

    text: str
    """The concatenated corpus text, ready to be embedded in juror prompts."""

    files: list[str]
    """Relative paths of the files that contributed to the corpus, in load order.
    Surfaced in the dashboard so users see exactly which sources grounded
    the eval."""

    total_chars: int
    """Total characters loaded (== len(text), pre-truncation)."""

    truncated: bool
    """True if `corpus.max_chars` was hit and more source files were skipped."""

    skipped: list[str]
    """Files NOT loaded because the budget filled before reaching them.
    Empty if `truncated` is False."""


def load_corpus(corpus: KnowledgeCorpus) -> LoadedCorpus:
    """Load a KnowledgeCorpus from disk + inline_text override.

    Resolution order:
      1. If `corpus.inline_text` is set, return that as a single-entry corpus.
         (Useful for tests + smoke runs that don't want disk I/O.)
      2. Otherwise walk `corpus.sources` in declaration order. For each entry:
         * If it's a file, load it.
         * If it's a directory, walk recursively and load every file whose
           extension is in `corpus.extensions`.
      3. Stop when `corpus.max_chars` is hit. Record what was skipped.

    Always returns a LoadedCorpus — never raises on missing files (they're
    silently skipped + logged as warnings via the runner's event stream).
    The result's `text` may be empty if nothing was loadable.
    """
    if corpus.inline_text is not None:
        text = corpus.inline_text[:corpus.max_chars]
        return LoadedCorpus(
            text=text,
            files=["<inline>"],
            total_chars=len(text),
            truncated=len(corpus.inline_text) > corpus.max_chars,
            skipped=[],
        )

    # Normalize extension list — accept ".md" or "md".
    exts = {(e if e.startswith(".") else f".{e}").lower() for e in corpus.extensions}

    files: list[Path] = []
    for src in corpus.sources:
        p = Path(src).expanduser()
        if not p.exists():
            continue  # silently skip — the runner emits a warning event
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            # Recursive walk; deterministic order via sorted() so the same
            # corpus produces the same prompt across runs (reproducible).
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in exts:
                    files.append(f)

    # Load up to the char budget. Deterministic: stop mid-corpus rather than
    # mid-file so users can predict what was included.
    parts: list[str] = []
    loaded_paths: list[str] = []
    skipped_paths: list[str] = []
    total = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            skipped_paths.append(str(f))
            continue
        # Header: relative path if possible, full path otherwise.
        header = f"--- {f.name} ---"
        try:  # noqa: SIM105
            # Best-effort: relative to current working directory.
            header = f"--- {f.resolve().relative_to(Path.cwd().resolve())} ---"
        except Exception:
            pass

        block = f"{header}\n{content}\n"
        if total + len(block) > corpus.max_chars:
            # Take only the portion that fits.
            remaining = corpus.max_chars - total
            if remaining > len(header) + 50:    # leave room for at least a header + a few chars
                truncated_block = block[:remaining]
                parts.append(truncated_block)
                loaded_paths.append(str(f))
                total += len(truncated_block)
            else:
                skipped_paths.append(str(f))
            # Everything after this file is skipped.
            for rest in files[files.index(f) + 1:]:
                skipped_paths.append(str(rest))
            break

        parts.append(block)
        loaded_paths.append(str(f))
        total += len(block)

    text = "\n".join(parts)
    return LoadedCorpus(
        text=text,
        files=loaded_paths,
        total_chars=len(text),
        truncated=bool(skipped_paths),
        skipped=skipped_paths,
    )
