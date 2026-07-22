# Governance upload — gate CI/CD on the release decision

The harness runs **fully local by default**. When you want a release gate, add
`--upload`: the harness POSTs the completed evaluation to the **ProofAgent
Governance API**, the API runs its gate engine against your governance profile,
and the harness exits with a code your pipeline can act on.

Every `--upload` run goes to **ProofAgent Cloud** (`https://app.proofagent.ai`)
by default; set `PROOFAGENT_API_BASE_URL` to point the CLI at an
Enterprise / on-prem backend instead.

> **On-prem / Enterprise?** The same CLI works — export
> `PROOFAGENT_API_BASE_URL=https://proofagent.acme.internal` (or pass
> `upload_run(api_url=…)` from Python).

> Terminology: the model that reviews the agent under test is the **harness
> LLM**. The Governance API never sees your harness-LLM credentials — only the
> resulting `Report`.

---

## Three ways to run

### 1. Local-only (default)

No network, no account, no data leaves your machine. This is the plain harness:

```bash
proof run my_agent.py \
  --role "airline customer support agent" \
  --turns 12
```

Exit code: `0` unless the certification is `NOT_READY` (then `1`). Use this for
local iteration.

### 2. Local + upload (manual)

Run locally **and** push the result to the Governance API to see it on the
dashboard. You only need an **API key** — the base URL defaults to ProofAgent
Cloud (`https://app.proofagent.ai`):

```bash
export PROOFAGENT_API_KEY="pa_live_..."          # the only thing required for Cloud

proof run my_agent.py \
  --role "airline customer support agent" \
  --upload --source manual --fail-on block \
  --agent airline-support --agent-version "$(git rev-parse --short HEAD)" \
  --profile airline_customer_support
```

### 3. CI/CD (gate the build)

The same command with `--source ci_cd` (the default). The process exit code is
the gate decision — let it fail the job. See the GitHub Actions example below.

---

## Environment variables

| Variable                   | CLI flag      | Purpose                                                        |
| -------------------------- | ------------- | ------------------------------------------------------------- |
| `PROOFAGENT_API_KEY`       | `--api-key`   | API key for the Governance API. **Required** for `--upload`.  |
| `PROOFAGENT_EVIDENCE`      | _none_        | `0` disables evidence-driven findings (on by default).        |
| `PROOFAGENT_EVIDENCE_LLM`  | _none_        | Model used to structure finding evidence (default `gpt-4.1-mini`). |
| `PROOFAGENT_COMPLIANCE`    | `--assess-compliance` | Truthy (`1`) opts in to the compliance assessment without the flag. **Off by default.** |
| `PROOFAGENT_COMPLIANCE_FRAMEWORKS` | `--frameworks` | Comma-separated framework ids to assess (same scope override as `--frameworks`). |

The CLI flag wins over the environment variable when both are set. The base URL
defaults to ProofAgent Cloud, so only the **API key** is required. If `--upload`
is given with no key, the harness prints a clear error and exits non-zero (it
does **not** silently skip the gate).

---

## Evidence-driven findings

On upload, each finding is enriched into **actionable, evidence-driven bullets**
instead of prose. For every finding the harness produces:

- a one-line **summary** of the concrete failure,
- **bullets** of `claim → artifact line ref → contradicting source + line`, and
- a **fix recommendation**.

The governance dashboard renders these natively (claim, "Claimed at" line, a red
"Contradicts" line with the source ref, and a green Recommendation callout).

This runs as one LLM call per finding (capped at 8) at upload time, using the
artifact text + knowledge corpus (artifact mode) or the transcript (multi-turn)
as grounding. It is **best-effort and no-op-safe**: if the model is unavailable
or a call fails, the finding keeps its existing prose — the gate decision is
never affected.

- **On by default.** Set `PROOFAGENT_EVIDENCE=0` to skip it (e.g. air-gapped
  runs where the evidence model isn't reachable).
- **Model:** `PROOFAGENT_EVIDENCE_LLM` (default `gpt-4.1-mini`). Use a small,
  cheap model — this is structuring, not scoring.

---

## Compliance assessment (opt-in: `--assess-compliance`)

A **post-jury compliance-assessor node** maps the finished evaluation to the
regulatory frameworks that govern the agent — a per-control status (`met` /
`partial` / `attention` / `not_evaluated`) plus a why-not-compliant / proof /
fix per control, using the jury's findings as evidence — and attaches it to the
report (`report.compliance`). It travels in the report and the upload payload,
so the **governance platform only displays it and never calls a model**.

- **Opt-in, off by default.** Enable with `--assess-compliance` (or
  `PROOFAGENT_COMPLIANCE=1`). **One harness-LLM call covering all selected
  frameworks**; it never affects the metric scores, certification, or the gate.
  No-op-safe: if no harness LLM is configured or the call fails, the frameworks
  simply render as a neutral "not assessed".
- **Scope resolution** (which frameworks are assessed): `--frameworks a,b,c`
  always wins (fully local); otherwise the Agent Governance Profile's frameworks
  (below), when one is loaded; otherwise the platform profile's selection, fetched
  via `GET /compliance/selection` when an API key is present; otherwise the
  local default core set (pure open source, no network call).
- Rendered in the report Markdown and shown on the governance **Compliance** page
  (with each framework's coverage %, control statuses, and rationale).

---

## Local governance gate (`--governance-profile` / `--assess-governance`)

The gate does not require the cloud. An **Agent Governance Profile** — one YAML
block declaring the agent's risk context (use case, autonomy level, data
sensitivity, region, human oversight, consequential actions) — is run through the
same deterministic risk classifier the dashboard uses, and the derived **tier
guardrails gate the release locally** with the same exit codes as the table
below: score floor per tier, block on finding severity, sign-off tiers stop at
`review`, prohibited use cases always block.

```bash
# governance as code: the profile lives in your repo, the gate runs on your machine
proof run my_agent.py --governance-profile governance.yaml --fail-on block
```

Precedence: a `--governance-profile` file wins; `--assess-governance` instead
pulls the profile bound to `--agent NAME` from the dashboard (best-effort — an
offline run proceeds without it); with neither, the evaluation runs exactly as
before. With a profile attached the whole run is governed by the classification:
the adversarial evaluation targets the declared risk, `--assess-context` is held
to the tier's bar, and `--assess-compliance` is scoped to the profile's
frameworks (see the README's **Agent Governance Profile** section for the full
YAML anatomy and tier guardrails).
With `--upload`, the profile travels in the payload as
`agent_governance_profile` and fills the agent's risk classification on the
dashboard.

---

## All upload flags

| Flag                  | Default                         | Meaning                                                           |
| --------------------- | ------------------------------- | ---------------------------------------------------------------- |
| `--upload/--no-upload`| `--no-upload`                   | Turn the gate on.                                                |
| `--api-key`           | `$PROOFAGENT_API_KEY`           | API key. **Required** for `--upload`.                           |
| `--agent`             | falls back to `--role`          | Logical agent name — groups runs and powers regression checks.  |
| `--agent-version`     | _none_                          | Version / git ref of the agent under test.                      |
| `--profile`           | _none_                          | Governance profile slug to evaluate against.                    |
| `--fail-on`           | `block`                         | Which decision fails the build: `pass` \| `review` \| `block`.  |
| `--source`            | `ci_cd`                         | Run origin: `local` \| `ci_cd` \| `manual` \| `api` \| `scheduled`. |
| `--environment` / `--env` | _none_                      | Deployment environment recorded on the run: `development` \| `staging` \| `production`. Governance uses it for release decisions + workflow matching. |

---

## Exit codes

The Governance API returns a `gate_status`; the harness maps it to a process
exit code so CI can gate on it:

| Gate decision | Exit code | Meaning                                                                 |
| ------------- | --------- | ---------------------------------------------------------------------- |
| `pass`        | **0**     | Release allowed.                                                       |
| `review`      | **1**     | Soft gate — needs a human. Exit `1` **only** with `--fail-on review`; with the default `--fail-on block` a `review` is informational (exit `0`). |
| `block`       | **2**     | Hard gate — release blocked. Always exit `2`, regardless of `--fail-on`. |

`--fail-on` controls strictness:

- `--fail-on block` (default): only a `block` fails the build.
- `--fail-on review`: both `review` **and** `block` fail the build.
- `--fail-on pass`: never fails on `review`; a `block` still exits `2`.

When `--upload` succeeds, the harness prints the decision, the final score and
grade, any `failed_rules`, and the `dashboard_url`.

---

## Artifact mode

The same gate works on a finished **artifact** — generated code, a BRD, a
technical spec, a report, a plan — with `proof artifact --upload`. Score the
deliverable against a knowledge corpus and gate on the result:

```bash
export PROOFAGENT_API_KEY="pa_live_..."          # base URL defaults to Cloud

proof artifact ./proposal.md \
  --type BRD \
  --knowledge-dir ./docs \
  --llm gpt-4.1-mini \
  --upload --source ci_cd --fail-on block \
  --agent analyst-brd --agent-version "$(git rev-parse --short HEAD)" \
  --profile artifact_governance_default
```

`build_governance_payload` detects the mode from the `Report`, so the payload
carries the artifact section (section scores, unsupported claims, missing
requirements) instead of a turn-by-turn transcript. Everything else — flags,
exit codes, gate semantics, the Cloud/Enterprise URL switch — is identical to a
multi-turn run.

---

## From Python (no CLI)

`--upload` is sugar over three public functions in
`proofagent_harness.governance`. Call them directly when you run the harness
from Python — the mechanism is the same for both modes:

```python
import os, sys
from proofagent_harness import Harness
from proofagent_harness.governance import (
    build_governance_payload, upload_run, gate_exit_code, GovernanceUploadError,
)

# 1. Run the eval (multi-turn shown; artifact mode is identical from step 2 on —
#    Harness(mode="artifact").evaluate(artifact=..., knowledge_corpus=...)).
report = Harness(llm="gpt-4.1-mini", turns=12).evaluate(
    my_agent, role="airline customer support agent",
)

# 2. Map the Report to the governance run-upload contract.
payload = build_governance_payload(
    report,
    agent_name="airline-support",     # groups runs + powers regressions
    agent_version="1.4.0",            # git ref of the agent under test
    profile="airline_customer_support",
    source="ci_cd",                  # local | ci_cd | manual | api | scheduled
)

# 3. Upload + gate. api_url defaults to ProofAgent Cloud — pass it only for an
#    Enterprise / on-prem endpoint (api_url="https://proofagent.acme.internal").
try:
    decision = upload_run(payload, api_key=os.environ["PROOFAGENT_API_KEY"])
except GovernanceUploadError as exc:
    print(f"upload failed: {exc}")
    sys.exit(2)

print(decision["gate_status"], "→", decision.get("dashboard_url"))
sys.exit(gate_exit_code(decision["gate_status"], fail_on="block"))
```

Every runnable example does this in one line via the bundled
[`examples/_dashboard.py`](../examples/_dashboard.py) helper, which is env-gated
and no-op-safe (the run stays fully offline until you set `PROOFAGENT_API_KEY`):

```python
from _dashboard import push_to_dashboard
push_to_dashboard(report, agent_name="my-agent", profile="my_profile")
```

---

## GitHub Actions example

```yaml
name: Agent governance gate

on:
  pull_request:
    branches: [main]

jobs:
  governance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install the harness
        run: |
          python -m pip install --upgrade pip
          pip install proofagent-harness

      - name: Evaluate + gate on the governance decision
        env:
          PROOFAGENT_API_KEY: ${{ secrets.PROOFAGENT_API_KEY }}
          # The harness LLM credentials (kept on the runner, never uploaded).
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          proof run my_agent.py \
            --role "airline customer support agent" \
            --turns 12 \
            --upload \
            --source ci_cd \
            --fail-on block \
            --agent airline-support \
            --agent-version "${GITHUB_SHA::7}" \
            --profile airline_customer_support
```

A `block` decision exits `2` and fails the job; `pass` exits `0` and the merge
proceeds. Switch `--fail-on review` once your team is ready to also gate on
soft-review decisions.
