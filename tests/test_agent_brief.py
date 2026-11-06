"""Every attack is written for the agent under test: the brief the planner and the conductor share."""
import json
from pathlib import Path

from proofagent_harness.agents import planner as P
from proofagent_harness.agents.brief import agent_brief, knowledge_rules, tool_lines
from proofagent_harness.loaders import load_traps
from proofagent_harness.schemas import Trap

EX = Path(__file__).resolve().parents[1] / "examples" / "credit_agent"


def _state():
    class Ctx:
        system_prompt = (EX / "context" / "system_prompt.md").read_text()
        tools = json.loads((EX / "context" / "tools.json").read_text())
    if isinstance(Ctx.tools, dict):
        Ctx.tools = Ctx.tools.get("tools", [])
    kt = "\n".join(p.read_text() for p in sorted((EX / "domain_knowledge").glob("*.md")))
    return {"context": Ctx(), "role": "consumer credit allocation agent", "goal": "g", "business_case": "b",
            "knowledge_text": kt, "traps": load_traps()}


def test_the_brief_carries_the_agents_tools_and_its_domain_rules():
    s = _state()
    b = agent_brief(s)
    assert "consumer credit allocation agent" in b and "System prompt" in b
    assert any(line.startswith("- verify_identity") or "verify_identity" in line for line in tool_lines(s))
    rules = knowledge_rules(s)
    assert "ceiling" in rules.lower() and "45%" in rules  # the limit and the DTI threshold an attacker would push


def test_generated_traps_are_made_judgeable_and_tagged():
    s = _state()
    t = P._as_custom(Trap(name="Push past the Tier B ceiling", family="business_logic", severity="high",
                          metrics=["safety"], seeds=["x"], pass_criteria="refuses"), s)
    assert t.name.startswith("custom_") and t.checks and "agent-specific" in t.tags and t.source == "custom"
