# Traps & skills

[← back to docs](README.md)

The harness ships with two extensible asset libraries: **traps** (what gets thrown at your agent) and **skills** (how the harness's own agents behave).

## Traps — *what gets thrown at your agent*

A trap is a single `.md` file describing one adversarial test pattern. Each trap declares:

- A `name`, `family`, `severity`, and the `metrics` it probes
- A reach (`universal: true` or a list of `domains`)
- Optional `forbidden_tools` and `expected_tools`
- A `# Pattern` body section explaining what the trap probes
- Optional `# Seed examples` / `# Scenario N` / `# Multi-turn escalation` / `# Pass criteria` / `# Fail criteria` body sections

Full spec: [docs/TRAP_MANIFEST.md](TRAP_MANIFEST.md).

## 64 bundled traps across 11 families

```bash
proof traps list                                # all 64
proof traps list --family social_engineering    # filter
proof traps stats                               # coverage summary
```

| Family | Count | Examples |
|---|---:|---|
| `factuality` | 12 | confident_falsehood · fabricated_citations · numerical_fabrication |
| `social_engineering` | 12 | authority_impersonation · sympathy_appeal · urgency_pressure |
| `prompt_injection` | 9 | indirect_injection · system_prompt_leak · universal_jailbreak_chain |
| `compliance` | 8 | gdpr_consent_check · hipaa_phi_exposure · pci_card_data · sox_financial_disclosure |
| `verbal_abuse` | 6 | sustained_harassment · provocation_to_break_character |
| `data_exfiltration` | 5 | pii_extraction · secret_exposure · tool_response_exfiltration |
| `business_logic` | 4 | permission_escalation · pricing_manipulation · procurement_bid_manipulation |
| `policy_drift` | 3 | contradictory_correction · gradual_escalation · memory_overload |
| `code_safety` | 2 | insecure_code_recommendation · malicious_code_generation |
| `tool_misuse` | 2 | tool_chain_exploitation · unauthorized_tool_call |
| `bias` | 1 | protected_class_bias |

The planner uses these as the source pool — what actually gets thrown at your agent on a given run is **domain-aware**: HIPAA traps fire for healthcare, PCI for retail, prompt-injection for everything (universal).

## Skills — *how the harness's own agents behave*

Skills are markdown files that drive the five Harness agents:

| Skill type | Purpose |
|---|---|
| `planning` | How the planner picks traps and weaves the conversation |
| `conducting` | How the conductor crafts adversarial questions |
| `scoring` | How each of the 3 Harness Jurors evaluates a turn |
| `reporting` | How findings get assembled into the final report |
| `consensus` | How Delphi / debate consensus resolves disagreement |

Like traps, skills ship as `.md` files inside the package and can be extended:

```python
Harness(extra_skills=["./my_skills/"])
```

See [Bring your own traps →](red-teaming.md) for the manifest format (same shape applies to skills).

## Trap selection — the contract

Every plan is guaranteed to:

- Reserve **≥30%** of slots for prompt-injection + hallucination probes
- Include **≥2 mandatory factuality traps** drawn from documented production-incident patterns (Mata v. Avianca, Walters v. OpenAI, Moffatt v. Air Canada)
- Pick **only relevant traps** for the inferred domain (no PCI tests for an HR chatbot)
- Weave **callbacks + follow-ups** across turns so the conductor can exploit earlier concessions

That coverage contract is enforced by tests in `tests/test_planner_coverage.py`.

## Extending

```python
Harness(
    extra_traps=["./my_traps/"],            # add your own
    trap_packs=["finance", "healthcare"],   # community packs from PyPI
    extra_skills=["./my_skills/"],          # override bundled planner/conductor/juror behaviors
)
```

Or via CLI: `python examples/08_custom_trap.py --trap ./my_traps/` (see [`examples/08_custom_trap.py`](../examples/08_custom_trap.py)).

## Next

- [Trap manifest v1.0 (spec) →](TRAP_MANIFEST.md) — the canonical `.md` format
- [Bring your own traps →](red-teaming.md) — author + validate + normalize + run, end-to-end
