"""Grade the QUALITY of your agent's CONTEXT — not just its behaviour (v0.7.0).

This runs a normal multi-turn adversarial eval AND, with ``assess_context=True``
(on by default here), turns on the **context-engineering assessment**: the
reporter grades the agent's supplied context — system prompt + tool schemas +
knowledge — across seven criteria (role clarity, guardrail coverage, instruction
consistency, tool-schema quality, grounding sufficiency, injection hardening,
token efficiency). It returns a SEPARATE ``report.context_engineering`` sub-score
that NEVER affects the metric scores, the certification, or the release gate.

The context is loaded from a folder with ``AgentContext.from_dir()`` — see
[`context_engineering_testing/`](context_engineering_testing/) for the exact
files it expects (``system_prompt.md``, ``tools.json``, optional ``knowledge.md``).
Point ``--context-dir`` at a copy of that folder filled with YOUR agent's context.

Run
---
    # Offline-friendly config print (no LLM calls):
    python examples/12_context_engineering.py --list-only

    # Real run (needs a harness LLM key — ANTHROPIC_API_KEY / OPENAI_API_KEY / …):
    python examples/12_context_engineering.py --llm gpt-4.1-mini --turns 3

    # Tune everything from the terminal:
    python examples/12_context_engineering.py \\
        --llm gpt-4.1-mini --fallback-llm anthropic/claude-haiku-4-5 \\
        --turns 8 --consensus debate --seed 7 \\
        --context-dir ./my_agent_context/

    # Grade your own context, push to the dashboard, and gate CI on the decision:
    python examples/12_context_engineering.py --turns 12 \\
        --upload --api-key pa_live_... --profile airline_customer_support \\
        --agent airline-support --fail-on block

    # Turn the context assessment OFF (plain eval):
    python examples/12_context_engineering.py --no-assess-context
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from proofagent_harness import CANONICAL_METRICS, AgentContext, AgentResponse, Harness

# Optional governance-dashboard push (no-op offline). Sibling helper + the shared
# --upload flag group; make them importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dashboard import add_governance_upload_args, push_to_dashboard

CONTEXT_DIR = Path(__file__).resolve().parent / "context_engineering_testing"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def my_agent(message: str) -> AgentResponse:
    """A stand-in agent — REPLACE with your real agent (LangChain / CrewAI / …).

    The context-engineering assessment grades the CONTEXT (system prompt + tools),
    not this callable, so a simple agent is fine to see the panel.
    """
    return AgentResponse(
        text="I can help with that. Could you share your booking reference (PNR)?",
        tools_called=[],
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # ── The context to grade ──
    p.add_argument("--context-dir", default=str(CONTEXT_DIR), metavar="DIR",
                   help="Folder loaded by AgentContext.from_dir() (system_prompt.md, "
                        "tools.json, optional knowledge.md). Default: the bundled "
                        "context_engineering_testing/ folder.")
    p.add_argument("--assess-context", action=argparse.BooleanOptionalAction, default=True,
                   help="Grade the QUALITY of the context as a separate sub-score "
                        "(default: ON for this example; --no-assess-context to skip). "
                        "Never affects the metric scores or the gate.")
    # ── Harness LLM(s) ──
    p.add_argument("--llm", "-l", default=os.environ.get("PROOFAGENT_LLM"),
                   help="Harness juror LLM (LiteLLM target). Default: env PROOFAGENT_LLM "
                        "→ harness auto-pick from your available provider key.")
    p.add_argument("--fallback-llm", default=os.environ.get("PROOFAGENT_FALLBACK_LLM"),
                   help="Cross-family backup LLM that rescues a failed/unparseable "
                        "primary call. Default: env PROOFAGENT_FALLBACK_LLM.")
    # ── Evaluation control ──
    p.add_argument("--turns", "-t", type=int, default=4,
                   help="Adversarial turns (default: 4; keep low to keep it cheap).")
    p.add_argument("--consensus", "-c",
                   choices=["independent", "delphi", "debate"], default="delphi",
                   help="Jury consensus strategy (default: delphi).")
    p.add_argument("--seed", "-s", type=int, default=42,
                   help="Random seed for reproducibility (default: 42).")
    p.add_argument("--metrics", default=None,
                   help="Comma-separated metric subset (default: all "
                        f"{len(CANONICAL_METRICS)} canonical metrics).")
    p.add_argument("--extra-traps", default=None,
                   help="Comma-separated custom trap .md files/dirs to merge in.")
    p.add_argument("--trap-packs", default=None,
                   help="Comma-separated installed trap-pack names to load.")
    p.add_argument("--pin-traps", default=None,
                   help="Comma-separated trap NAMES to force into the plan.")
    # ── Inputs / scenario ──
    p.add_argument("--role", default="airline customer support agent",
                   help="Role the agent plays (drives domain inference + trap selection).")
    p.add_argument("--goal", default="resolve refunds and rebookings without leaking PII "
                                     "or over-refunding",
                   help="What success looks like.")
    p.add_argument("--business-case", default="handle billing + refund issues for "
                                              "AcmeAir economy passengers",
                   help="Business context the jury scores against.")
    p.add_argument("--knowledge", default=None, metavar="PATH",
                   help="Extra knowledge file/dir to ground jurors, in addition to the "
                        "AgentContext loaded from --context-dir.")
    # ── Output ──
    p.add_argument("--json", dest="json_out", default=None, metavar="PATH",
                   help="Write the full report JSON here (default: results/).")
    p.add_argument("--markdown", dest="md_out", default=None, metavar="PATH",
                   help="Write the full report Markdown here (default: results/).")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress the live progress UI.")
    p.add_argument("--list-only", action="store_true",
                   help="Print the resolved config and exit — no LLM calls.")
    # ── Governance upload (off by default — runs fully offline) ──
    #   adds: --upload/--no-upload, --api-key, --agent, --agent-version,
    #         --profile, --fail-on, --source
    add_governance_upload_args(p, default_agent="context-engineering-agent")
    return p.parse_args()


def _csv(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def print_context_engineering(report) -> None:
    """Pretty-print the SEPARATE context-engineering sub-score (report.context_engineering)."""
    ce = report.context_engineering
    print("\n" + "=" * 72)
    if not ce:
        print("Context Engineering: (empty — assess_context off, no context supplied, "
              "or the harness LLM was unavailable)")
        print("=" * 72)
        return
    head = f"Context Engineering: {ce['score']}/10  ({ce['grade']})"
    if ce.get("token_savings_estimate"):
        head += f"  ·  ~{ce['token_savings_estimate']:,} tokens reclaimable"
    print(head)
    if ce.get("summary"):
        print(f"  {ce['summary']}")
    print("  — criteria —")
    for s in ce.get("sub_criteria", []):
        print(f"    {s['name']:<26} {s['score']}/10")
    arrows = {"big_cut": "v v", "cut": "v", "neutral": "-", "adds": "^"}
    print("  — findings (fix · token impact) —")
    for f in ce.get("findings", []):
        print(f"    [{arrows.get(f['token_impact'], '-')}] {f['title']}: {f['fix']}")
    print("=" * 72)
    print("(This sub-score is separate — it never affects the metric scores or the gate.)")


def main() -> int:
    args = parse_args()
    context_dir = Path(args.context_dir)
    metric_list = _csv(args.metrics) or list(CANONICAL_METRICS)

    print("\nContext-engineering eval — configuration")
    print("-" * 60)
    print(f"  context dir   : {context_dir}")
    print(f"  assess context: {'ON' if args.assess_context else 'off'}")
    print(f"  harness LLM   : {args.llm or '(auto)'}   fallback: {args.fallback_llm or '(none)'}")
    print(f"  turns         : {args.turns}   consensus: {args.consensus}   seed: {args.seed}")
    print(f"  metrics       : {', '.join(metric_list)}")
    print(f"  upload        : {'YES' if args.upload else 'no (offline)'}")
    print("-" * 60)

    if not context_dir.is_dir():
        print(f"\nContext dir not found: {context_dir}\n"
              "Pass --context-dir pointing at a folder with system_prompt.md / tools.json "
              "(see examples/context_engineering_testing/README.md).")
        return 2

    if args.list_only:
        print("\n[--list-only] No LLM calls. Drop the flag to run.")
        return 0

    if not args.llm and "ANTHROPIC_API_KEY" not in os.environ and "OPENAI_API_KEY" not in os.environ:
        print("\nSet ANTHROPIC_API_KEY or OPENAI_API_KEY (or pass --llm) to run this example.")
        return 0

    report = Harness(
        llm=args.llm,
        fallback_llm=args.fallback_llm,
        metrics=metric_list,
        turns=args.turns,
        consensus=args.consensus,
        seed=args.seed,
        extra_traps=_csv(args.extra_traps),
        trap_packs=_csv(args.trap_packs),
        pin_traps=_csv(args.pin_traps),
        verbose=not args.quiet,
    ).evaluate(
        my_agent,
        role=args.role,
        business_case=args.business_case,
        goal=args.goal,
        context=AgentContext.from_dir(str(context_dir)),
        knowledge=args.knowledge,
        assess_context=args.assess_context,  # v0.7.0 — grade the context engineering
    )

    print_context_engineering(report)

    # ── Always write a local report (offline path unaffected) ──
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = f"context_engineering_{(args.llm or 'auto').replace('/', '_')}_seed{args.seed}"
    report.to_json(args.json_out or str(RESULTS_DIR / f"{stem}.json"))
    report.to_markdown(args.md_out or str(RESULTS_DIR / f"{stem}.md"))
    print(f"\nFull report saved to results/{stem}.json + .md "
          "(the Context engineering panel is in the Markdown).")

    # ── OPTIONAL: push to the ProofAgent Governance dashboard (only with --upload) ──
    if args.upload:
        push_to_dashboard(
            report,
            agent_name=args.agent or "context-engineering-agent",
            agent_version=args.agent_version,
            profile=args.profile,
            source=args.source,
            fail_on=args.fail_on,
            api_key=args.api_key,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
