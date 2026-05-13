# Contributing to proofagent-harness

Thanks for considering a contribution. The two highest-leverage things you can do
are:

1. **Submit a new trap** — even one well-crafted markdown file makes the harness
   sharper for everyone.
2. **Submit a new persona** — a different juror voice helps the consensus
   surface failure modes the rigorous/lenient/contrarian trio misses.

Both are pure markdown — no Python required.

## Quickstart for code contributions

```bash
git clone https://github.com/proofagent/proofagent-harness
cd proofagent-harness
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

If `pytest` is green, you're good to go.

## Project layout

```
src/proofagent_harness/
├── harness.py              # the public Harness facade
├── schemas.py              # Pydantic models — public API
├── llm.py                  # BYO LLM wrapper (LiteLLM)
├── loaders.py              # skill/trap/persona/knowledge loaders
├── progress.py             # Rich progress UI
├── cli.py                  # Typer CLI
├── graph/                  # LangGraph orchestration
├── agents/                 # The 5 pipeline agents (one node each)
├── tools/                  # Tool implementations
├── consensus/              # Consensus strategies
├── scoring/                # Scoring aggregator (deterministic)
└── data/
    ├── skills/             # built-in skill markdown
    ├── personas/           # built-in persona markdown
    └── traps/              # built-in trap markdown (organized by family)

tests/
├── conftest.py             # FakeLLM + shared fixtures
├── test_schemas.py
├── test_loaders.py
├── test_scoring.py
└── test_harness_integration.py   # full-pipeline test (uses FakeLLM)

examples/                   # runnable scripts
notebooks/                  # Jupyter / Colab walkthroughs
```

## How to add a new trap

1. Pick the family folder under `src/proofagent_harness/data/traps/`.
   If your trap is genuinely a new family (e.g., `accessibility/`), add a folder.
2. Create a markdown file with frontmatter:
   ```markdown
   ---
   name: my_attack_unique_name
   family: social_engineering
   severity: high
   metrics: [safety, manipulation_resistance]
   tags: [my-tag]
   ---

   # Pattern
   What kind of adversarial scenario this is.

   # Seed examples
   - "The actual message the conductor will send."
   - "Another seed."

   # Pass criteria
   What the agent should do.

   # Fail criteria
   What constitutes failure.
   ```
3. Add a test in `tests/test_loaders.py` that asserts your new trap loads.
4. Run `pytest tests/test_loaders.py` — green? You're ready to PR.

## How to add a new persona

1. Add `src/proofagent_harness/data/personas/<name>.md` with frontmatter:
   ```markdown
   ---
   name: my_persona
   description: "One-line summary of stance."
   ---

   # My persona
   Markdown body describing the stance — injected into the juror's prompt.
   ```
2. Add an integration test that uses `personas=['rigorous', 'my_persona', 'lenient']`.
3. PR.

## How to add a new metric

This is a more substantial change. You'll need to:

1. Add the metric to `CANONICAL_METRICS` in `schemas.py` (and `METRIC_DESCRIPTIONS`).
2. Add a scoring rubric file at `data/skills/scoring/<metric>.md`.
3. Update relevant traps to declare the metric in their `metrics: [...]` list.
4. Add tests.

Open an issue first to discuss — we want to keep the canonical list to ~5
universal metrics. Domain-specific metrics belong in trap packs, not core.

## Code style

- **Ruff** for lint + format: `ruff check src tests && ruff format src tests`
- **Pyright** for types: `pyright src`
- All public APIs use **Pydantic v2 models**.
- Tests use **pytest + pytest-asyncio**, with the `FakeLLM` fixture from
  `conftest.py` — no real LLM calls in CI.

## PR checklist

- [ ] Tests pass (`pytest`)
- [ ] No new ruff or pyright errors (`ruff check . && pyright src`)
- [ ] If you added a public API: it's exported from `proofagent_harness/__init__.py`
- [ ] If you added bundled assets: there's a loader test that asserts they load
- [ ] CHANGELOG.md updated under `## Unreleased`

## Code of conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md).
Be kind. Assume good faith. Disagree on the substance, not the person.

Report unacceptable behavior to **conduct@proofagent.com**.

## Reporting security issues

Don't file public issues for security reports. See [SECURITY.md](SECURITY.md)
— email **security@proofagent.com**.

## Issue templates

We use three templates:
- **Bug report** — a run failed, scores look wrong
- **Feature request** — new trap / rubric / conductor technique
- **Calibration concern** — score doesn't match your expectation of the agent

GitHub will route you to the right one when you open a new issue.

## Continuous integration

PRs trigger CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) which:
- Runs `pytest` on Python 3.10, 3.11, 3.12 (Ubuntu)
- Lints with `ruff`
- Type-checks with `pyright` (warn-only — heuristic models break it)
- Verifies all `examples/` and `benchmarks/` parse
- Confirms bundled markdown (skills/personas/traps) loads cleanly

## Releasing to PyPI (manual, from your terminal)

There is **no auto-publish workflow** — releases are cut by hand from a
trusted machine so we can pause, inspect the build, and promote multiple
release candidates without GitHub round-trips.

```bash
# one-time setup
pip install -e ".[dev]"                # installs build + twine
# create ~/.pypirc OR export TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-...

# tag the release commit (hatch-vcs reads the tag for the version)
git tag v0.2.0

# dry-run on TestPyPI first
scripts/release.sh --test

# real release
scripts/release.sh
```

`scripts/release.sh` runs ruff + pytest, cleans `dist/`, builds wheel +
sdist, sanity-checks the wheel includes bundled traps/skills/personas,
runs `twine check`, then uploads. Use `--build-only` to inspect artifacts
without uploading, or `--skip-checks` to skip ruff/pytest when you've
already run them. `--help` shows all flags.

After a successful release, push the tag: `git push --tags`.

## Discrimination-impact check (recommended for scoring/conductor changes)

If your PR touches the **planner / conductor / juror / scoring** logic,
include before-and-after numbers from the calibration benchmark in your
PR description:

```bash
python benchmarks/calibration_check.py --turns 15 --consensus delphi
```

The PR template has a section for this. Goal: discrimination gap stays
≥ 3.0 between the hardened and weak agents.
