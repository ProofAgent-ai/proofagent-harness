# Bring your own traps

[← back to docs](README.md)

The whole point of an extensible test harness: contribute your own adversarial patterns. This is the end-to-end workflow.

## Workflow

```
1. Author       →  2. Validate      →  3. Normalize    →  4. Run
   .md file        proof traps          scripts/          Harness(extra_traps=[...])
                   validate             normalize_traps     or examples/08_custom_trap.py
```

## 1. Author

A trap is a single `.md` file with YAML frontmatter + Markdown sections. Full spec: [docs/TRAP_MANIFEST.md](TRAP_MANIFEST.md). Minimal example:

```markdown
---
name: my_attack
family: social_engineering
severity: high
metrics: [safety, manipulation_resistance]
tags: [my-tag]
universal: true              # or: domains: [retail, support]
forbidden_tools: [send_link]
---

# Pattern
What the trap probes and why it's hard.

# Seed examples
- "Realistic message the conductor uses as a starting point."
- "Another seed — the conductor mixes and adapts these per turn."

# Pass criteria
- What the agent should do.

# Fail criteria
- What constitutes failure.
```

The bundled library has 64 worked examples — read [`src/proofagent_harness/data/traps/`](../src/proofagent_harness/data/traps/) for inspiration. The richer the trap (more scenarios, more rich sections like `# Auto-scoring signals` or `# Ideal response pattern`), the better the Harness Jurors can ground their reasoning.

### Two valid styles, one schema

- **Simple style** (e.g. `permission_escalation`) — `# Seed examples` + `# Pass criteria` + `# Fail criteria`
- **Scenario style** (e.g. `policy_evolution_claim`) — multiple `# Scenario 1 — title` blocks, each with its own turns + expected behavior + hard-fail criteria

Both validate cleanly. Pick whichever fits your attack — short multi-turn scripts work better as scenarios; reusable single-turn probes work better as seeds.

## 2. Validate before shipping

```bash
proof traps validate path/to/your_trap.md          # one file
proof traps validate path/to/your_traps_dir/       # a directory
proof traps validate                               # bundled library
proof traps validate --strict                      # warnings = errors (CI)
```

The validator separates **errors** (hard violations that block use) from **warnings** (style guidance you can address later). Exit code is non-zero on errors — and on any warning when `--strict` is set.

### Common findings + fixes

| Symptom | Fix |
|---|---|
| `severity: severe` | Use `low / medium / high / critical` |
| `metrics: [policy_compliance]` | Use one of the 5 canonical metrics or a registered alias |
| Missing reach | Add `universal: true` OR a non-empty `domains:` list |
| Missing `# Pattern` | Add a `# Pattern` body block |
| No demo block | Add `# Seed examples` OR `# Scenario N` OR `# Multi-turn escalation` |
| Non-canonical key order | Optional — the normalizer handles it (see below) |

### Quick-iterate while authoring

```bash
cat > /tmp/test_trap.md <<'EOF'
---
name: my_attack
family: social_engineering
severity: high
metrics: [safety, manipulation_resistance]
universal: true
---

# Pattern
Free-form description of the attack.

# Seed examples
- "Realistic starting point."
EOF

proof traps validate /tmp/test_trap.md
```

## 3. Normalize to canonical form

Frontmatter key ordering + section-header alias rewriting, with built-in semantic-equality verification (every loaded `Trap` object stays byte-identical):

```bash
python scripts/normalize_traps.py --dry-run        # show what would change
python scripts/normalize_traps.py                  # apply + verify
python scripts/normalize_traps.py --check          # CI: exit 1 if not canonical
```

The normalizer **only changes structure, never content** — it asserts every loaded `Trap` is byte-identical before vs after.

## 4. Run

### Via the Python API

```python
from proofagent_harness import Harness

Harness(
    extra_traps=["./my_traps/"],          # adds your traps to the bundled pool
    # OR
    trap_packs=["finance", "healthcare"], # community packs from PyPI
).evaluate(my_agent, role="...", goal="...")
```

### Via the bundled example script

[`examples/08_custom_trap.py`](../examples/08_custom_trap.py) is a focused demo that pairs the bundled trap library with your custom dir + supports the full LLM-choice flags (agent model, juror model, optional proxy juror for local LLMs):

```bash
# Sanity check — loads bundled + custom trap, no API calls
python examples/08_custom_trap.py --list-only

# Default Claude agent + Claude juror + bundled chargeback trap
python examples/08_custom_trap.py --turns 8

# Pick agent + juror models (any LiteLLM target)
python examples/08_custom_trap.py --turns 8 \
    --agent-model claude-haiku-4-5 --llm gpt-4.1

# Route the JUDGE to a local mlx / vllm / lm-studio proxy
python examples/08_custom_trap.py --turns 8 \
    --agent-model claude-haiku-4-5 \
    --proxy-url http://127.0.0.1:1234/v1 \
    --llm gemma-4-e4b-it-mlx --ctx 6000

# Point at your own trap directory
python examples/08_custom_trap.py --turns 8 --trap path/to/your_traps/
```

The example ships with a worked sample trap at [`examples/custom_traps/refund_chargeback_threat.md`](../examples/custom_traps/refund_chargeback_threat.md) so the script runs end-to-end with no extra setup.

## Accumulation behavior

When you add a custom trap dir, **bundled traps stay loaded**. The merge rule:

- Different name → both kept (additive)
- Same name → custom wins (intentional — lets you patch a bundled trap)
- Never subtractive — you can't accidentally remove a bundled trap

Verify with `Harness(extra_traps=[...])` then `load_trap_index(extra_dirs=[...]).stats()` — bundled count stays at 64, the custom adds on top.

## Contribute upstream

If your trap could benefit the community, open a PR:

```bash
git checkout -b add-trap-my-attack
cp /path/to/your_trap.md src/proofagent_harness/data/traps/<family>/
proof traps validate src/proofagent_harness/data/traps/<family>/your_trap.md --strict
pytest
# open a PR — see CONTRIBUTING.md
```

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full contributor guide.

## Next

- [Trap manifest v1.0 (spec) →](TRAP_MANIFEST.md) — full reference for the `.md` format
- [Traps & skills overview →](traps.md) — what traps and skills are, the 11 bundled families
