#!/usr/bin/env python3
"""Artifact-mode smoke test — runnable end-to-end check.

Loads the bundled sample artifact + knowledge corpus from this folder,
runs Harness(mode="artifact").evaluate(...), and prints the result. Use
this script to:

  1. Verify the SDK installation works (no API key needed for --list-only)
  2. Confirm artifact mode runs against a real LLM (default: OpenAI
     gpt-4.1-mini — fast, cheap, reliable JSON output)
  3. Confirm Live Reporting streams to your dashboard (with PROOFAGENT_API_KEY)
  4. Sanity-check edge cases (corrupt the bundled artifact, watch scores drop)

Usage
-----

  # Dry run — no API calls; confirms loaders + schemas work.
  python testing/artifact/run_artifact_test.py --list-only

  # Real eval against OpenAI gpt-4.1-mini (default).
  export OPENAI_API_KEY=sk-...
  python testing/artifact/run_artifact_test.py

  # With Live Reporting (streams to proofagent.ai dashboard).
  export OPENAI_API_KEY=sk-...
  export PROOFAGENT_API_KEY=apk_live_...
  python testing/artifact/run_artifact_test.py --live

  # Swap in a different juror LLM (any LiteLLM-supported model).
  python testing/artifact/run_artifact_test.py --llm claude-haiku-4-5
  python testing/artifact/run_artifact_test.py --llm gpt-5

  # Swap in your own artifact + knowledge folder.
  python testing/artifact/run_artifact_test.py \\
      --artifact path/to/your/artifact.md \\
      --knowledge path/to/your/knowledge_folder/

  # Negative control — corrupt the artifact, see scores drop.
  python testing/artifact/run_artifact_test.py --corrupt

Expected on the bundled (well-grounded) artifact:
  - final_score: 8.0-9.5 (the bundled plan matches the brief tightly)
  - certification: GOLD or SILVER
  - findings: 0 or a small number of low-severity items
  - 4 metrics scored: task_success, hallucination_resistance,
    instruction_following, safety
  - manipulation_resistance NOT scored (auto-dropped in artifact mode)

Expected with --corrupt (TAM swapped, Risks section deleted):
  - final_score: 4.0-6.5
  - certification: NEEDS_ENHANCEMENT or NOT_READY
  - findings: multiple items, including hallucination on TAM number +
    instruction_following on the missing Risks section
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the SDK importable when running from a checkout (`pip install -e .`
# is recommended; this falls back when the package is installed elsewhere).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.exists() and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from proofagent_harness import AgentArtifact, Harness, KnowledgeCorpus   # noqa: E402
from proofagent_harness.schemas import ARTIFACT_METRIC_DESCRIPTIONS       # noqa: E402

DEFAULT_ARTIFACT  = HERE / "sample_artifact" / "q3_plan.md"
DEFAULT_KNOWLEDGE = HERE / "sample_knowledge"
RESULTS_DIR       = HERE / "results"


ROLE = "strategy drafting assistant for the LATAM expansion committee"
BUSINESS_CASE = (
    "produce a complete, accurate, on-policy Q3 LATAM market-entry plan "
    "grounded in the executive brief and supporting market research; "
    "required sections: Executive Summary, Market Analysis, Pricing & "
    "Packaging, GTM Plan, Risks, Financial Projections, Timeline"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--artifact", "-a", type=str, default=str(DEFAULT_ARTIFACT),
        help=f"Path to artifact (.md/.txt/.pdf/.docx/.html). Default: bundled q3_plan.md",
    )
    p.add_argument(
        "--knowledge", "-k", type=str, default=str(DEFAULT_KNOWLEDGE),
        help=f"Path to knowledge folder. Default: bundled sample_knowledge/",
    )
    p.add_argument(
        "--llm", "-l", type=str, default="gpt-4.1-mini",
        help="Harness juror LLM. Default: gpt-4.1-mini (fast, cheap, reliable JSON). "
             "Any LiteLLM-supported model works (e.g. claude-haiku-4-5, gpt-5, "
             "gemini-2.0-flash). Requires the matching provider API key in env.",
    )
    p.add_argument("--seed", "-s", type=int, default=42)
    p.add_argument(
        "--live", action="store_true",
        help="Stream to proofagent.ai dashboard (requires PROOFAGENT_API_KEY).",
    )
    p.add_argument(
        "--corrupt", action="store_true",
        help="Inject hallucinations into the bundled artifact (TAM, missing section). "
             "Use to verify scores actually drop — sanity check the eval pipeline.",
    )
    p.add_argument(
        "--list-only", action="store_true",
        help="Print the test plan and exit — no API calls.",
    )
    return p.parse_args()


def corrupt_artifact_text(text: str) -> str:
    """Swap the TAM figure + delete the Risks section to verify the jury catches both."""
    # 1. Hallucinate a TAM number (brief says $2.4B; we say $24B).
    text = text.replace("$2.4B", "$24B")
    # 2. Delete the entire Risks section (brief explicitly REQUIRES it).
    if "## Risks" in text and "## Financial Projections" in text:
        before = text.split("## Risks")[0]
        after = "## Financial Projections" + text.split("## Financial Projections")[1]
        text = before + after
    return text


def main() -> int:
    args = parse_args()

    artifact_path  = Path(args.artifact).expanduser().resolve()
    knowledge_path = Path(args.knowledge).expanduser().resolve()

    if not artifact_path.exists():
        print(f"[error] artifact not found: {artifact_path}", file=sys.stderr)
        return 2

    # Load the artifact (auto-converts PDF/DOCX/HTML).
    artifact = AgentArtifact.from_path(artifact_path, type="business_plan")

    # Negative-control mode: corrupt the artifact so we can verify scores drop.
    if args.corrupt:
        artifact = AgentArtifact(
            generated_artifact=corrupt_artifact_text(artifact.generated_artifact),
            type="business_plan",
            source_path=str(artifact_path),
            metadata={"corrupted_for_testing": True},
        )
        print("[smoke-test] CORRUPT mode: TAM swapped + Risks section deleted")

    corpus: KnowledgeCorpus | None = None
    if knowledge_path.exists():
        corpus = KnowledgeCorpus(
            sources=[str(knowledge_path)],
            extensions=[".md", ".txt"],
            max_chars=200_000,
        )

    print("\n[smoke-test] artifact mode plan")
    print(f"  artifact      : {artifact_path}")
    print(f"                  {len(artifact.generated_artifact):,} chars, type={artifact.type}")
    if corpus is not None:
        print(f"  knowledge dir : {knowledge_path}")
    else:
        print(f"  knowledge dir : <missing> — will run without corpus")
    print(f"  juror LLM     : {args.llm}")
    print(f"  seed          : {args.seed}")
    print(f"  live reporting: {'YES' if args.live else 'no'}")
    print(f"  corrupt mode  : {'YES (negative control)' if args.corrupt else 'no'}")
    print(f"  metrics       : 4 (manipulation_resistance auto-dropped)")
    print(f"  expected score: " + (
        "4.0-6.5 (NEEDS_ENHANCEMENT)" if args.corrupt
        else "8.0-9.5 (GOLD or SILVER)"
    ))

    if args.list_only:
        print("\n[smoke-test] --list-only set; not calling LLM. Drop --list-only to run.")
        return 0

    if args.live and not os.environ.get("PROOFAGENT_API_KEY"):
        print(
            "\n[warn] --live set but PROOFAGENT_API_KEY is missing — Live Reporting "
            "will be silently disabled. Get a key at https://www.proofagent.ai/dashboard",
            file=sys.stderr,
        )

    # Run the eval.
    print("\n[smoke-test] running evaluation...\n")
    report = Harness(
        mode="artifact",
        llm=args.llm,
        consensus="delphi",
        seed=args.seed,
        live_reporting=args.live,
    ).evaluate(
        artifact=artifact,
        knowledge_corpus=corpus,
        role=ROLE,
        business_case=BUSINESS_CASE,
        tools_used=["web_search", "internal_kb"],
    )

    # Persist + print.
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = "smoke_test_corrupt" if args.corrupt else "smoke_test_clean"
    out_json = RESULTS_DIR / f"{stem}.json"
    out_md   = RESULTS_DIR / f"{stem}.md"
    report.to_json(str(out_json))
    report.to_markdown(str(out_md))

    print(f"\n[smoke-test] === RESULTS ===")
    print(f"  Final score   : {report.final_score:.2f}/10")
    print(f"  Certification : {report.certification}")
    print(f"  Report mode   : {report.mode}")
    print(f"  Tokens used   : {report.tokens_used:,}")
    print(f"  Duration      : {report.duration_seconds:.1f}s")

    # Per-metric scores WITH their artifact-mode definitions — so users
    # immediately see what each number means without re-reading the README.
    print(f"\n  Per-metric scores (with artifact-mode definitions):")
    for metric, score in sorted(report.per_metric.items()):
        desc = ARTIFACT_METRIC_DESCRIPTIONS.get(metric, "")
        print(f"    {metric:30s} {score:.2f}/10")
        if desc:
            # Wrap the description so the terminal output stays readable.
            wrapped = desc[:200] + ("…" if len(desc) > 200 else "")
            print(f"      └─ {wrapped}")

    # Jury panel — surface which jurors scored this run.
    personas = report.metadata.get("personas") if report.metadata else None
    if personas:
        print(f"\n  Jury panel ({len(personas)} strict artifact-mode jurors):")
        for p in personas:
            print(f"    - {p}")

    print(f"\n  Findings ({len(report.findings)}):")
    if not report.findings:
        print("    (none — jury found no actionable issues)")
    for i, f in enumerate(report.findings[:5], 1):
        print(f"    {i}. [{f.severity}] {f.headline}")
        if f.detail:
            wrapped = f.detail[:180] + ("…" if len(f.detail) > 180 else "")
            print(f"       {wrapped}")

    print(f"\n  Full report (JSON): {out_json}")
    print(f"  Full report (MD)  : {out_md}")

    # Light sanity check — return non-zero exit if results don't match expectation.
    if args.corrupt and report.final_score > 7.5:
        print("\n[smoke-test] FAIL — expected corrupted artifact to score below 7.5", file=sys.stderr)
        return 1
    if not args.corrupt and report.final_score < 6.0:
        print("\n[smoke-test] WARN — clean artifact scored unexpectedly low. "
              "Check the juror's reasoning in the JSON output.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
