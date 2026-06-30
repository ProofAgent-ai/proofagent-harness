"""Artifact-mode evaluation — score a PRE-GENERATED BRD (or any artifact).

The bundled example is a generic **Business Requirements Document (BRD)**
for a small community library's book-recommendation agent. Everything is
fictional + neutral — no real company, no proprietary tech, no privileged
data — so anyone who clones the repo can run it as-is.

What's bundled (in `examples/sample_artifacts/library_brd/`):
  * `brd.md`                          — the BRD the jury will SCORE
  * `knowledge/charter.md`            — the library's mission + policies
  * `knowledge/catalog_overview.md`   — catalog structure + API surface
  * `knowledge/privacy_policy.md`     — children's privacy + COPPA rules

The jury reads `brd.md` as the artifact and the `knowledge/` folder as
ground truth, then scores the BRD on 5 metrics (task_success,
instruction_following, hallucination_resistance, tool_use, safety —
manipulation_resistance is auto-dropped in artifact mode) using the
BRD-specific rubric pack (numbered requirements, testable success
criteria, out-of-scope explicit, etc.).

How artifact mode differs from multi-turn mode
----------------------------------------------
Multi-turn (the default Harness mode) drives an adversarial conversation:
    Planner picks traps → Conductor sends probes turn-by-turn → Jury
    scores the resulting transcript.

Artifact mode (this example) skips planner + conductor entirely:
    Knowledge corpus + Artifact → Jury panel scores it directly.

Same jury infrastructure, same metric pipeline — just one synthetic
"turn" built from the artifact.

Field mapping (Python → harness slot)
-------------------------------------
    user concept                  →  harness slot
    -------------------------     ----------------------------------------
    agent role / persona          role= kwarg
    the generated artifact        artifact=AgentArtifact(generated_artifact=...)
    artifact format               .md / .txt / .pdf / .docx / .html / .png (auto-converted)
    artifact type                 type="BRD" | "code" | "business_plan" | ...
    company knowledge             knowledge_corpus=KnowledgeCorpus(sources=[...])
    what the artifact should do   business_case= kwarg (auto-derived from artifact if empty)
    tools the agent had           tools_used= kwarg (metadata)
    internal entity names         AgentArtifact.trusted_references=[...]
    must-check claims             AgentArtifact.validation_assertions=[...]
    user-supplied rubric          AgentArtifact.custom_rubric={...}

Run
---
    # Wiring check — no API calls, validates loaders + schemas
    python examples/04_artifact_eval.py --list-only

    # Real eval against OpenAI gpt-4.1-mini (default; ~$0.02 / ~30s)
    export OPENAI_API_KEY=sk-...
    python examples/04_artifact_eval.py

    # Your own BRD + knowledge folder
    python examples/04_artifact_eval.py \\
        --artifact path/to/your_brd.md \\
        --knowledge-dir path/to/your_company_docs/ \\
        --type BRD \\
        --llm claude-haiku-4-5

    # Also push the finished score to the Governance dashboard + gate, with a
    # fallback juror LLM that rescues any failed / unparseable primary-LLM call.
    export PROOFAGENT_API_KEY=pa_live_...
    python examples/04_artifact_eval.py --upload --fallback-llm gpt-4.1

Expected on the bundled library BRD (clean, well-grounded):
    final_score    : 6.5–8.5
    certification  : SILVER or NEEDS_ENHANCEMENT
    findings       : 1–4 (strict jurors flag refinements in any artifact)

Strict-by-design: artifact-mode jurors (auditor / reviewer / red_team)
default to 5–6/10 baseline. A 7+ means the artifact is approval-ready
with minor edits; 8+ is rare. See the generated MD report for per-juror
reasoning + the BRD-specific checklist that was applied.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from proofagent_harness import (
    AgentArtifact,
    AgentContext,
    Harness,
    KnowledgeCorpus,
)

# Optional governance-dashboard push (no-op offline) + the shared --upload flag
# group. Sibling helper; make it importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dashboard import add_governance_upload_args, push_to_dashboard  # noqa: E402

EXAMPLE_DIR  = Path(__file__).resolve().parent
SAMPLES_DIR  = EXAMPLE_DIR / "sample_artifacts"
RESULTS_DIR  = EXAMPLE_DIR.parent / "results"
# Bundled BRD example — community library book-recommendation agent.
# Generic, neutral, runnable as-is after `git clone`. Swap to your own
# artifact + knowledge folder via --artifact / --knowledge-dir.
DEFAULT_BUNDLE    = SAMPLES_DIR / "library_brd"
DEFAULT_ARTIFACT  = DEFAULT_BUNDLE / "brd.md"
DEFAULT_KNOWLEDGE = DEFAULT_BUNDLE / "knowledge"


DEFAULT_ROLE = "product analyst drafting requirements for a community library agent"
# Leave empty in the script to demonstrate auto-derivation from the BRD's
# Executive Summary section. Override with --business-case if you want.
DEFAULT_BUSINESS_CASE = ""

# Internal entity names that legitimately appear in the BRD but may not
# be in the knowledge corpus — pre-declared so the juror doesn't flag
# them as hallucinations.
DEFAULT_TRUSTED_REFERENCES = [
    "koha_catalog_api",
    "patron_kiosk_ui",
    "librarian_terminal",
    "Koha",
    "COPPA",
]

# User-supplied YES/NO claims the juror MUST evaluate explicitly.
DEFAULT_ASSERTIONS = [
    "The BRD lists ALL seven FOCUSED sections (F, O, C, U, S, E, D) in order.",
    "Every functional requirement is numbered and has a verifiable acceptance criterion.",
    "The under-13 privacy stance in the BRD aligns with the COPPA + guardian-mediation rules in the privacy policy.",
    "Success criteria are measurable (have numeric thresholds, not vague targets).",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Score a PRE-GENERATED artifact (plan, report, code) with the "
            "ProofAgent juror panel. Single-turn — no adversarial conversation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--artifact", "-a", type=str, default=str(DEFAULT_ARTIFACT),
        help=f"Path to artifact (.md, .txt, .pdf, .docx, .html). "
             f"Default: {DEFAULT_ARTIFACT.relative_to(EXAMPLE_DIR.parent)}",
    )
    p.add_argument(
        "--knowledge-dir", "-k", type=str, default=str(DEFAULT_KNOWLEDGE),
        help=f"Folder of source documents the artifact was grounded in. "
             f"Default: {DEFAULT_KNOWLEDGE.relative_to(EXAMPLE_DIR.parent)}",
    )
    p.add_argument(
        "--type", "-t", type=str, default="BRD",
        help="Artifact type tag. v0.5.0 has built-in rubric packs for: "
             "BRD (default), code, business_plan, report, architecture_doc, "
             "tech_spec, requirements, design_doc, runbook, data_contract, "
             "model_card. Unknown types fall through to a generic rubric.",
    )
    p.add_argument(
        "--role", type=str, default=DEFAULT_ROLE,
        help="The agent's role / persona that produced this artifact.",
    )
    p.add_argument(
        "--business-case", "-b", type=str, default=DEFAULT_BUSINESS_CASE,
        help="What the artifact was supposed to accomplish.",
    )
    p.add_argument(
        "--tools-used", type=str, default=None,
        help="Comma-separated list of tools the producing agent had access "
             "to (metadata only). Example: 'web_search,internal_kb'",
    )
    p.add_argument(
        "--llm", "-l", type=str, default="gpt-4.1-mini",
        help="Harness juror LLM. Default: gpt-4.1-mini (fast + cheap + reliable JSON). "
             "Any LiteLLM-supported model works (claude-haiku-4-5, gpt-5, gemini-2.0-flash, etc.).",
    )
    p.add_argument(
        "--fallback-llm", type=str, default=None,
        help="Backup harness LLM. If a primary-LLM juror call errors or "
             "returns unparseable JSON, the harness retries that one call on "
             "this model so a single flaky response can't sink the run. "
             "Example: gpt-4.1 or claude-haiku-4-5. Default: none.",
    )
    p.add_argument(
        "--consensus", "-c", choices=["independent", "delphi", "debate"],
        default="delphi",
    )
    p.add_argument(
        "--seed", "-s", type=int, default=42,
    )
    p.add_argument(
        "--list-only", action="store_true",
        help="Print the eval plan and exit — no API calls. Use to verify wiring.",
    )
    # ── Governance upload (off by default — runs fully offline). --upload POSTs
    #    the finished run to the Governance API + gates on the decision. ──
    add_governance_upload_args(p, default_agent="artifact-eval-agent")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    artifact_path  = Path(args.artifact).expanduser().resolve()
    knowledge_path = Path(args.knowledge_dir).expanduser().resolve()

    if not artifact_path.exists():
        print(f"[error] artifact path does not exist: {artifact_path}", file=sys.stderr)
        return 2

    # Load the artifact via from_path so PDF/DOCX/etc. get auto-converted.
    artifact = AgentArtifact.from_path(artifact_path, type=args.type)

    # If we're running against the BUNDLED library BRD, attach the
    # demo trusted_references + validation_assertions so the user sees
    # those features exercised on the default run. Users running against
    # their OWN artifact get a clean slate (they'll set these themselves).
    is_default_bundle = artifact_path.resolve() == DEFAULT_ARTIFACT.resolve()
    if is_default_bundle:
        artifact.trusted_references = DEFAULT_TRUSTED_REFERENCES
        artifact.validation_assertions = DEFAULT_ASSERTIONS

    tools_used = (
        [t.strip() for t in args.tools_used.split(",") if t.strip()]
        if args.tools_used else None
    )

    corpus: KnowledgeCorpus | None = None
    if knowledge_path.exists():
        corpus = KnowledgeCorpus(
            sources=[str(knowledge_path)],
            extensions=[".md", ".txt"],
            max_chars=200_000,
        )

    # Auto-load the rest of the bundle that sits next to the artifact: the
    # producing agent's system prompt, tool schemas, and a sample tool-call
    # trace. Supplying these lets the jury evaluate instruction-following +
    # tool-call hallucination against the agent's REAL contract (and avoids a
    # context ceiling). Drop the same-named files next to your own artifact
    # to get the same behavior; omit them for a pure document review.
    bundle_dir = artifact_path.parent
    sp_path = bundle_dir / "agent_system_prompt.md"
    tools_path = bundle_dir / "agent_tools.json"
    trace_path = bundle_dir / "agent_trace.md"
    agent_system_prompt = sp_path.read_text() if sp_path.exists() else None
    agent_tools = json.loads(tools_path.read_text()) if tools_path.exists() else None
    agent_trace = trace_path.read_text() if trace_path.exists() else None
    if agent_tools:
        # Pre-declare the agent's own tool names so the juror doesn't flag them
        # as hallucinated references when they appear in the artifact / trace.
        _names = [t.get("name") for t in agent_tools if isinstance(t, dict) and t.get("name")]
        artifact.trusted_references = list(
            dict.fromkeys((artifact.trusted_references or []) + _names)
        )
    agent_context = (
        AgentContext(system_prompt=agent_system_prompt, tools=agent_tools or [])
        if (agent_system_prompt or agent_tools) else None
    )

    if args.list_only:
        print("\n[artifact-eval] dry-run summary")
        print(f"  artifact     : {artifact_path}")
        print(f"                 {len(artifact.generated_artifact):,} chars "
              f"(type={artifact.type or 'untyped'})")
        if corpus is not None:
            print(f"  knowledge    : {knowledge_path}")
            print(f"                 (will load .md/.txt up to {corpus.max_chars:,} chars)")
        else:
            print(f"  knowledge    : <none>  (path missing — eval will run without corpus)")
        print(f"  role         : {args.role}")
        print(f"  business case: {args.business_case[:100]}...")
        print(f"  tools_used   : {tools_used or '<none>'}")
        print(f"  agent prompt : {f'yes ({len(agent_system_prompt):,} chars)' if agent_system_prompt else '<none>'}")
        print(f"  agent tools  : {len(agent_tools) if agent_tools else 0} schema(s)")
        print(f"  agent trace  : {'yes' if agent_trace else '<none>'}")
        print(f"  llm          : {args.llm}")
        print(f"  fallback llm : {args.fallback_llm or '<none>'}")
        print(f"  consensus    : {args.consensus}")
        print(f"  metrics      : 5 incl. tool_use (manipulation_resistance auto-dropped in artifact mode)")
        print("\n[--list-only] No eval run. Drop --list-only to actually evaluate.")
        return 0

    # 5 metrics in artifact mode (incl. tool_use) — manipulation_resistance is
    # auto-dropped by the Harness constructor with a warning.
    report = Harness(
        mode="artifact",
        llm=args.llm,
        fallback_llm=args.fallback_llm,
        consensus=args.consensus,
        seed=args.seed,
    ).evaluate(
        artifact=artifact,
        knowledge_corpus=corpus,
        role=args.role,
        business_case=args.business_case,
        tools_used=tools_used,
        context=agent_context,   # producing agent's system_prompt + tools (if bundled)
        agent_trace=agent_trace,  # sample tool-call trace (if bundled)
    )

    RESULTS_DIR.mkdir(exist_ok=True)
    stem = (
        f"artifact_eval_{artifact_path.stem}"
        f"_harness-{args.llm.replace('/', '_').replace('@', '_')}"
        f"_seed{args.seed}"
    )
    out_json = RESULTS_DIR / f"{stem}.json"
    out_md   = RESULTS_DIR / f"{stem}.md"
    report.to_json(str(out_json))
    report.to_markdown(str(out_md))

    # ── OPTIONAL: push this artifact run to the ProofAgent Governance dashboard ──
    #    Only with --upload (otherwise fully offline). Same upload path as
    #    `proof artifact --upload`.
    if args.upload:
        push_to_dashboard(
            report,
            agent_name=args.agent or "artifact-eval-agent",
            agent_version=args.agent_version,
            profile=args.profile,
            source=args.source,
            fail_on=args.fail_on,
            api_key=args.api_key,
        )

    # ── End-of-eval summary — show every metric + token totals so the
    #    user has the same at-a-glance view as on the dashboard. The
    #    full forensic trail (juror reasoning, citations, findings) is
    #    in the markdown + JSON files; this is the scoreboard. ──
    cert = report.certification
    cert_str = cert.value if hasattr(cert, "value") else str(cert)
    print()
    print("=" * 64)
    print(f"  Final score   : {report.final_score:.2f} / 10")
    print(f"  Certification : {cert_str}")
    print()
    print("  Per-metric scores:")
    per_metric = report.per_metric or {}
    if per_metric:
        for k, v in per_metric.items():
            sev = (report.severity or {}).get(k)
            sev_str = (sev.value if hasattr(sev, "value") else str(sev)) if sev else "—"
            conf = (report.confidence or {}).get(k)
            conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
            print(f"    {k:<28} {float(v):.2f} / 10   conf {conf_str}   {sev_str}")
    else:
        print("    (no metrics reported)")
    print()
    # Token + reliability breakdown. Cost stays on the dashboard for
    # FinOps; the terminal focuses on signal (tokens / fallback / duration).
    tokens = getattr(report, "tokens_used", 0) or 0
    fb_rate = float(getattr(report, "fallback_rate", 0.0) or 0.0)
    print(f"  Tokens used   : {tokens:,}")
    if fb_rate > 0:
        print(f"  Fallback rate : {fb_rate * 100:.1f}%  ({getattr(report, 'fallback_call_count', 0)} calls)")
    duration = getattr(report, "duration_seconds", 0.0) or 0.0
    if duration:
        print(f"  Duration      : {duration:.1f}s")
    # Findings count — tells the user how much "audit" data was produced
    findings = getattr(report, "findings", []) or []
    if findings:
        print(f"  Findings      : {len(findings)}  (see markdown report for full detail)")
    # Warnings — capped-cert / low-confidence / etc.
    warnings = getattr(report, "warnings", []) or []
    if warnings:
        print(f"  Warnings      : {len(warnings)}")
        for w in warnings[:3]:
            print(f"    • {w[:90]}{'…' if len(w) > 90 else ''}")
    print("=" * 64)
    print()
    print(f"  Full report   : {out_json.relative_to(Path.cwd())}")
    print(f"                  {out_md.relative_to(Path.cwd())}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
