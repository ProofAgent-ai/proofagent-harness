# Security policy

## Reporting a vulnerability

ProofAgent Harness is security-evaluation tooling. We take vulnerability
reports seriously, including:

- Issues that could let a malicious **agent under test** influence the
  harness's scoring (output-injection, prompt-injection of the juror via
  the transcript, etc.)
- Issues that could let a malicious **trap or skill markdown file** execute
  code or escalate privileges when loaded
- Issues that could leak the operator's API keys, knowledge corpus, or
  other secrets into reports / logs / progress events
- Supply-chain issues with our dependencies

**Do not file public issues for security reports.** Please email
`security@proofagent.ai` with:

- A description of the vulnerability and its impact
- A minimal reproduction (transcript, trap file, code snippet)
- The harness version (`pip show proofagent-harness`) and Python version
- Any suggested mitigation

We aim to acknowledge within **2 business days** and provide an initial
triage within **5 business days**. Critical issues with a working exploit
will be fast-tracked.

## Coordinated disclosure

We follow responsible disclosure. After a fix is released:

1. We publish a `Security Advisory` on the repo (GitHub Security tab)
2. We credit the reporter (unless anonymity is requested)
3. We add the CVE / GHSA ID to the CHANGELOG
4. We notify users via the GitHub Releases RSS feed

## Scope

In scope:
- Code in this repository (`src/`, `examples/`, `benchmarks/`, `scripts/`)
- Bundled data files (`src/proofagent_harness/data/`)
- Documented public API (anything importable from `proofagent_harness`)

Out of scope:
- Vulnerabilities in user-supplied agents under test (that's what the
  harness is for — please file a public issue for findings)
- LLM provider issues (please report to the provider directly)
- Dependencies' vulnerabilities — please report upstream first; we'll bump
  versions promptly when fixes are available

## Hardening recommendations

If you're running ProofAgent in CI or against production agents:

- **Sandbox the harness's process** if loading user-contributed traps from
  untrusted directories — markdown frontmatter is parsed but not executed,
  but defense in depth is wise.
- **Don't commit `results/` to your repo** — eval reports may contain
  excerpts of agent responses including PII the conductor probed for. The
  bundled `.gitignore` excludes this directory by default.
- **Rotate the API keys** used for the harness LLM separately from your
  production agent's keys, in case a juror prompt accidentally captures
  agent-side credentials.
- **Use cross-family judges** (`--llm` from a different vendor than the
  agent) — same-model recognition bias can mask real failures.

## Security-relevant features

These features exist specifically to limit blast radius:

| Feature | Where |
|---|---|
| Pre-flight LLM reachability check (fails fast on auth issues) | `harness.py` |
| Per-turn agent crash isolation (one bad turn doesn't kill the run) | `conductor.py` |
| Fail-fast after N consecutive same-type agent crashes | `conductor.py` |
| Context-budget truncation (head/tail preserved) | `context_budget.py` |
| Tool-args fabrication detection (`phantom_tool_call_claimed`) | `conductor.py` |
| Indirect-injection detection in conducting skill | `data/skills/conducting.md` |
