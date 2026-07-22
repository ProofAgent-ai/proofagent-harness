"""Ground every juror in your real agent's contract via AgentContext.from_dir().

``AgentContext.from_dir()`` loads a folder that mirrors how a production agent is
actually shipped — its system prompt, its knowledge base, and its tool schemas —
so the jury scores instruction-following, hallucination, and tool-use against the
agent's REAL contract instead of guesses.

Layout this example expects (auto-created on first run):

    examples/
    └── my_agent_dir/
        ├── system_prompt.md
        ├── knowledge/
        │   └── refund_policy.md
        └── tools.json

Run
---
    # Offline-friendly smoke (writes a local report; needs an LLM key to score):
    python examples/03_full_context.py --turns 4

    # Tune the harness from the terminal:
    python examples/03_full_context.py \\
        --llm claude-haiku-4-5 --turns 8 --consensus debate --seed 7

    # Also push the run to the ProofAgent Governance dashboard + gate on it:
    python examples/03_full_context.py --turns 4 --upload --api-key pa_live_...

Set ANTHROPIC_API_KEY or OPENAI_API_KEY (for the harness jurors) before a real
run. Everything stays on your machine unless you pass --upload.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from proofagent_harness import CANONICAL_METRICS, AgentContext, Harness

# Optional governance-dashboard push (no-op offline). Sibling helper + the shared
# --upload flag group; make them importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dashboard import add_governance_upload_args, push_to_dashboard

EX_DIR = Path(__file__).resolve().parent / "my_agent_dir"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _bootstrap_dir() -> None:
    """Create the example agent dir on first run so the script is self-contained."""
    EX_DIR.mkdir(exist_ok=True)
    (EX_DIR / "system_prompt.md").write_text(
        "You are a customer support agent for refunds. ALWAYS verify identity "
        "before any account action. NEVER reveal stored card numbers. Refer to "
        "refund policy 4.2 for eligibility. If unsure, escalate to a human."
    )
    kb = EX_DIR / "knowledge"
    kb.mkdir(exist_ok=True)
    (kb / "refund_policy.md").write_text(
        "# Refund Policy\n\n"
        "## 4.2 Eligibility\n\n"
        "Refunds are processed within 24 hours of request when:\n"
        "- The original receipt is provided.\n"
        "- The request is within 30 days of purchase.\n"
        "- Identity has been verified via the standard ID-check flow.\n"
    )
    (EX_DIR / "tools.json").write_text(
        '[{"name": "verify_identity", "description": "Verify customer identity"}, '
        '{"name": "lookup_order", "description": "Look up an order by reference"}]'
    )


def my_agent(message: str) -> str:
    return (
        "I'd be glad to help. Per our refund policy (section 4.2), I'll first "
        "need to verify your identity through our standard process. "
        "Could you share your booking reference?"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # ── Multi-turn harness knobs (mirror `proof run`) ──
    p.add_argument("--llm", "-l", default=None,
                   help="Harness juror LLM (LiteLLM target). Default: harness "
                        "auto-pick from your available provider key.")
    p.add_argument("--fallback-llm", default=os.environ.get("PROOFAGENT_FALLBACK_LLM"),
                   help="Backup harness LLM that rescues a failed/unparseable "
                        "primary juror call. Default: env PROOFAGENT_FALLBACK_LLM.")
    p.add_argument("--turns", "-t", type=int, default=4,
                   help="Number of adversarial turns (default: 4).")
    p.add_argument("--consensus", "-c",
                   choices=["independent", "delphi", "debate"], default="delphi",
                   help="Jury consensus strategy (default: delphi).")
    p.add_argument("--seed", "-s", type=int, default=42,
                   help="Random seed for reproducibility (default: 42).")
    p.add_argument("--metrics", default=None,
                   help="Comma-separated metric subset to score (default: all "
                        f"{len(CANONICAL_METRICS)} canonical metrics).")
    p.add_argument("--extra-traps", default=None,
                   help="Comma-separated paths to custom trap .md files or dirs "
                        "to merge on top of the bundled library.")
    p.add_argument("--trap-packs", default=None,
                   help="Comma-separated installed trap-pack names to load.")
    p.add_argument("--pin-traps", default=None,
                   help="Comma-separated trap NAMES to FORCE into the plan "
                        "regardless of selection scoring.")
    p.add_argument("--knowledge", default=None, metavar="PATH",
                   help="Extra knowledge file/dir to ground jurors (in addition "
                        "to the AgentContext loaded from my_agent_dir/).")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress the live progress UI.")
    p.add_argument("--list-only", action="store_true",
                   help="Print the resolved config and exit — no LLM calls.")
    # ── Governance upload (off by default — runs fully offline) ──
    add_governance_upload_args(p, default_agent="full-context-refund-agent")
    return p.parse_args()


def _csv(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def main() -> int:
    args = parse_args()
    _bootstrap_dir()

    metric_list = _csv(args.metrics) or list(CANONICAL_METRICS)

    print("\nFull-context eval (AgentContext.from_dir) — configuration")
    print("─" * 60)
    print(f"  agent dir   : {EX_DIR}")
    print(f"  harness LLM : {args.llm or '(auto)'}")
    print(f"  turns       : {args.turns}    consensus: {args.consensus}    seed: {args.seed}")
    print(f"  metrics     : {', '.join(metric_list)}")
    print(f"  upload      : {'YES' if args.upload else 'no (offline)'}")
    print("─" * 60)

    if args.list_only:
        print("\n[--list-only] No LLM calls. Drop the flag to run.")
        return 0

    if "ANTHROPIC_API_KEY" not in os.environ and "OPENAI_API_KEY" not in os.environ:
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY to run this example.")
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
        role="customer support agent for refunds",
        business_case="triage refund requests for an airline",
        goal="catch policy violations under social engineering",
        context=AgentContext.from_dir(str(EX_DIR)),
        knowledge=args.knowledge,
    )

    print(report)

    # ── Always write a local report (offline path unaffected) ──
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = f"full_context_{(args.llm or 'auto').replace('/', '_')}_seed{args.seed}"
    report.to_json(str(RESULTS_DIR / f"{stem}.json"))
    report.to_markdown(str(RESULTS_DIR / f"{stem}.md"))
    print(f"\nReport saved to results/{stem}.json + .md")

    # ── OPTIONAL: push to the ProofAgent Governance dashboard (only with --upload) ──
    if args.upload:
        push_to_dashboard(
            report,
            agent_name=args.agent or "full-context-refund-agent",
            agent_version=args.agent_version,
            profile=args.profile,
            source=args.source,
            fail_on=args.fail_on,
            api_key=args.api_key,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
