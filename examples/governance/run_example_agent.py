"""Evaluate one of the bundled tool-calling example agents and upload it.

Loads a production-style spec from ``examples/agents/*.json`` (customer support,
medical triage, code generation, privacy/security) via the factory, runs the
adversarial multi-turn harness with the FULL AgentContext (system_prompt + tool
schemas + knowledge — so scores are NOT context-capped), then uploads the report
to the governance dashboard. The reporter's compliance assessment + evidence-
driven findings ride along in the upload.

Usage:
    export OPENAI_API_KEY=sk-...                       # harness + agent + compliance model
    export PROOFAGENT_API_KEY=pa_live_...              # YOUR workspace's API key
    python examples/governance/run_example_agent.py --agent customer_support_agent --turns 8

Agents: customer_support_agent | medical_triage_assistant | code_generation_agent | privacy_security_agent
Env knobs: HARNESS_LLM (default gpt-4.1-mini), HARNESS_FALLBACK_LLM, AGENT_MODEL
(default gpt-4.1-mini), AGENT_VERSION (default v1.0.0), FAIL_ON (default block),
PROOFAGENT_COMPLIANCE=0 to skip the compliance section.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from proofagent_harness import Harness

AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "agents"
sys.path.insert(0, str(AGENTS_DIR))
from factory import (  # noqa: E402
    load_agent_spec,
    make_agent_from_spec,
    make_context_from_spec,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent", "-a", default="customer_support_agent",
                   help="Spec stem in examples/agents/ (no .json).")
    p.add_argument("--llm", "-l", default=os.environ.get("HARNESS_LLM", "gpt-4.1-mini"))
    p.add_argument("--agent-model", default=os.environ.get("AGENT_MODEL", "gpt-4.1-mini"))
    p.add_argument("--turns", "-t", type=int, default=int(os.environ.get("TURNS", "8")))
    p.add_argument("--profile", "-p", default=None, help="Governance profile slug.")
    p.add_argument("--json", default=None, metavar="PATH",
                   help="Also save the full report JSON locally (still uploads).")
    p.add_argument("--markdown", "--md", default=None, metavar="PATH",
                   help="Also save the report Markdown locally (still uploads).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    spec_path = AGENTS_DIR / f"{args.agent}.json"
    if not spec_path.exists():
        print(f"No such agent spec: {spec_path}", file=sys.stderr)
        return 2

    spec = load_agent_spec(spec_path)
    agent = make_agent_from_spec(spec, args.agent_model)
    context = make_context_from_spec(spec)  # system_prompt + tools + knowledge

    print(f"Evaluating '{spec.name}' ({len(spec.tools_openai)} tools) — real LLM calls…")
    report = Harness(
        llm=args.llm,
        fallback_llm=os.environ.get("HARNESS_FALLBACK_LLM"),
        turns=args.turns,
        consensus="delphi",
    ).evaluate(
        agent,
        role=spec.role,
        business_case=spec.business_case,
        goal=spec.goal,
        context=context,
    )
    print(f"\nfinal_score = {report.final_score}   certification = {report.certification}")
    print(f"findings    = {len(report.findings)}   turns = {len(report.transcript)}")
    comp = getattr(report, "compliance", {}) or {}
    print(f"compliance  = {len(comp.get('frameworks', []))} frameworks "
          f"({'generated' if comp.get('generated') else 'empty'})")

    # Save a local copy if requested — independent of whether we upload.
    if args.json:
        report.to_json(args.json)
        print(f"saved JSON     → {args.json}")
    if args.markdown:
        report.to_markdown(args.markdown)
        print(f"saved Markdown → {args.markdown}")

    key = os.environ.get("PROOFAGENT_API_KEY")
    if not key:
        if not (args.json or args.markdown):
            out = pathlib.Path(__file__).parent / "_example_report.json"
            report.to_json(str(out))
            print(f"\nSaved {out}.")
        print("Set PROOFAGENT_API_KEY to upload (runs go to ProofAgent Cloud).")
        return 0

    from proofagent_harness.governance import (
        DEFAULT_API_BASE_URL,
        build_governance_payload,
        gate_exit_code,
        structure_findings_evidence,
        upload_run,
    )

    base = DEFAULT_API_BASE_URL

    payload = build_governance_payload(
        report,
        agent_name=spec.name,
        agent_version=os.environ.get("AGENT_VERSION", "v1.0.0"),
        profile=args.profile,
        source="manual",
    )
    if os.environ.get("PROOFAGENT_EVIDENCE", "1") != "0":
        transcript = "\n".join(
            f"[turn {i}] USER: {t.question}\n[turn {i}] AGENT: {t.answer}"
            for i, t in enumerate(report.transcript)
        )[:24000]
        structure_findings_evidence(
            payload, transcript=transcript,
            model=os.environ.get("PROOFAGENT_EVIDENCE_LLM", "gpt-4.1-mini"),
        )
    resp = upload_run(payload, api_url=base, api_key=key)
    print(f"\nGATE: {resp.get('gate_status')}  →  {resp.get('dashboard_url')}")
    if resp.get("failed_rules"):
        print("failed rules:", ", ".join(resp["failed_rules"]))
    return gate_exit_code(resp.get("gate_status", ""), os.environ.get("FAIL_ON", "block"))


if __name__ == "__main__":
    sys.exit(main())
