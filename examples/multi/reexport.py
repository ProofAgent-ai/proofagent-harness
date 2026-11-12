"""Re-export a saved run's PER exactly as `proof run` does (same context, knowledge artifacts), under the installed
eio-agents, and upload it through the same production path (`cli._upload_per_and_gate`). The conversation and the
jury's verdicts are not re-run: only the record is rebuilt, so it reflects the current EIO release for the same
evidence.

Usage: python reexport.py <run_dir> <agent> <version> [--upload]   (env: PROOFAGENT_API_BASE_URL, PROOFAGENT_API_KEY)
Always writes <run_dir>/run.per09.json; with --upload also sends it and prints the platform's reply."""
import json
import os
import sys
from pathlib import Path

from proofagent_harness import cli
from proofagent_harness.schemas import AgentContext, Report


def main(run_dir: str, agent: str, version: str, upload: bool) -> None:
    d = Path(run_dir)
    ctx_dir = Path("context") / agent
    ctx = AgentContext.from_dir(str(ctx_dir))
    report = Report.model_validate_json((d / "run.json").read_text())
    arts = cli.agent_artifacts(ctx, ctx_dir / "knowledge", report, agent)
    cli._export_per(report, d / "run.per09.json", agent_name=agent, agent_version=version,
                    system_prompt=getattr(ctx, "system_prompt", None), artifacts=arts)
    if upload:
        cli._upload_per_and_gate(report, api_key=os.environ.get("PROOFAGENT_API_KEY"), agent_name=agent,
                                 agent_version=version, fail_on="block", system_prompt=getattr(ctx, "system_prompt", None),
                                 exit_on_decision=False, artifacts=arts)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], "--upload" in sys.argv)
