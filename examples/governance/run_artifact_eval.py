"""Run a REAL artifact-mode governance evaluation and UPLOAD it to the dashboard.

Artifact mode scores a finished deliverable (here the bundled library BRD)
against its knowledge corpus — no conversation. This is the artifact counterpart
of ``run_governance_eval.py`` (multi-turn). It uploads the full report (findings
+ trace + tokens) to the ProofAgent Governance dashboard via POST /api/v1/runs
(the batch path your backend implements).

Usage:
    export OPENAI_API_KEY=sk-...                      # the harness LLM (jurors)
    export PROOFAGENT_API_BASE_URL=http://localhost:8000
    export PROOFAGENT_API_KEY=pa_live_...            # Dashboard → Settings → API keys
    python examples/governance/run_artifact_eval.py

Point it at your OWN deliverable:
    python examples/governance/run_artifact_eval.py \
        --artifact path/to/your.md --knowledge-dir path/to/docs/ --type BRD

Env knobs: HARNESS_LLM (default gpt-4.1-mini), HARNESS_FALLBACK_LLM,
AGENT_NAME (default "Library BRD Author"), AGENT_VERSION (default v1.0.0),
FAIL_ON (default block). Evidence-driven findings are ON by default —
PROOFAGENT_EVIDENCE=0 disables, PROOFAGENT_EVIDENCE_LLM picks the model.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from proofagent_harness import AgentArtifact, AgentContext, Harness, KnowledgeCorpus
from proofagent_harness.governance import (
    build_governance_payload,
    gate_exit_code,
    structure_findings_evidence,
    upload_run,
)

BUNDLE = pathlib.Path(__file__).resolve().parent.parent / "sample_artifacts" / "library_brd"

ROLE = "Business analyst authoring a BRD for a community library's book-recommendation agent"
BUSINESS_CASE = (
    "Produce a Business Requirements Document grounded in the library's charter, catalog, "
    "and children's-privacy policy: numbered, testable requirements; explicit out-of-scope; "
    "no unsupported claims; COPPA-safe."
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifact", "-a", default=str(BUNDLE / "brd.md"))
    p.add_argument("--knowledge-dir", "-k", default=str(BUNDLE / "knowledge"))
    p.add_argument("--type", "-t", default="BRD")
    p.add_argument("--llm", "-l", default=os.environ.get("HARNESS_LLM", "gpt-4.1-mini"))
    p.add_argument("--consensus", "-c", default="delphi", choices=["independent", "delphi", "debate"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    artifact_path = pathlib.Path(args.artifact).expanduser().resolve()
    knowledge_path = pathlib.Path(args.knowledge_dir).expanduser().resolve()

    artifact = AgentArtifact.from_path(artifact_path, type=args.type)
    corpus = (
        KnowledgeCorpus(sources=[str(knowledge_path)], extensions=[".md", ".txt"], max_chars=200_000)
        if knowledge_path.exists()
        else None
    )

    # Bundle the producing agent's contract (system prompt + tools + trace) when
    # present, so the jury grounds instruction-following / tool-call findings.
    bundle = artifact_path.parent
    sp = bundle / "agent_system_prompt.md"
    tp = bundle / "agent_tools.json"
    tr = bundle / "agent_trace.md"
    system_prompt = sp.read_text() if sp.exists() else None
    tools = json.loads(tp.read_text()) if tp.exists() else None
    agent_trace = tr.read_text() if tr.exists() else None
    context = (
        AgentContext(system_prompt=system_prompt, tools=tools or [])
        if (system_prompt or tools)
        else None
    )

    print("Running artifact evaluation (this makes real LLM calls)…")
    report = Harness(
        mode="artifact",
        llm=args.llm,
        fallback_llm=os.environ.get("HARNESS_FALLBACK_LLM"),
        consensus=args.consensus,
    ).evaluate(
        artifact=artifact,
        knowledge_corpus=corpus,
        role=ROLE,
        business_case=BUSINESS_CASE,
        context=context,
        agent_trace=agent_trace,
    )
    print(f"\nfinal_score = {report.final_score}   certification = {report.certification}")
    print(f"findings    = {len(report.findings)}")

    base = os.environ.get("PROOFAGENT_API_BASE_URL")
    key = os.environ.get("PROOFAGENT_API_KEY")
    if not (base and key):
        out = pathlib.Path(__file__).parent / "_artifact_report.json"
        report.to_json(str(out))
        print(f"\nSaved {out}. Set PROOFAGENT_API_BASE_URL + PROOFAGENT_API_KEY to upload.")
        return 0

    payload = build_governance_payload(
        report,
        agent_name=os.environ.get("AGENT_NAME", "Library BRD Author"),
        agent_version=os.environ.get("AGENT_VERSION", "v1.0.0"),
        profile="artifact_governance_default",
        source="manual",
    )

    # Evidence-driven findings: turn each finding into claim -> artifact line ref
    # -> contradicting source+line -> fix recommendation, so the dashboard shows
    # actionable bullets instead of prose. No-op-safe; PROOFAGENT_EVIDENCE=0 disables.
    if os.environ.get("PROOFAGENT_EVIDENCE", "1") != "0":
        knowledge_text = (
            "\n\n".join(p.read_text(errors="ignore") for p in sorted(knowledge_path.rglob("*.md")))[:24000]
            if knowledge_path.exists()
            else None
        )
        structure_findings_evidence(
            payload,
            artifact_text=artifact.generated_artifact,
            knowledge_text=knowledge_text,
            model=os.environ.get("PROOFAGENT_EVIDENCE_LLM", "gpt-4.1-mini"),
        )

    resp = upload_run(payload, api_url=base, api_key=key)
    print(f"\nGATE: {resp.get('gate_status')}  →  {resp.get('dashboard_url')}")
    if resp.get("failed_rules"):
        print("failed rules:", ", ".join(resp["failed_rules"]))
    return gate_exit_code(resp.get("gate_status", ""), os.environ.get("FAIL_ON", "block"))


if __name__ == "__main__":
    sys.exit(main())
