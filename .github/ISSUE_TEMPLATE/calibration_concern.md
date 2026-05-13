---
name: Calibration concern
about: A run produced a score that doesn't match your expectation of the agent
title: "[calibration] "
labels: calibration
---

## The discrepancy

<!-- "I ran X and got score Y, but I expected Z" — be concrete. -->

- **Agent under test:** <role + model>
- **Score reported:** <e.g. 9.6 GOLD>
- **Expected score / certification:** <e.g. should be ~7 because...>

## Did the calibration check pass?

```bash
python benchmarks/calibration_check.py --turns 15 --consensus delphi
```

- Hardened-agent score: ___
- Weak-agent score: ___
- Discrimination gap: ___ (≥3 = well-calibrated, 1.5-3 = some signal, <1.5 = not)

## Did the harness fire any warnings?

<!-- Plateau warning? Juror dissent? Limited-context cap? Defect counts?
The Report.warnings list often points at the cause directly. Paste relevant
ones here. -->

## What you've tried to rule out

- [ ] Cross-family judge (`--llm` from a different vendor than the agent)
- [ ] Different turn count (longer = more discrimination signal)
- [ ] `--consensus debate` (re-vote on dissent)
- [ ] `--repeats N` for variance reduction (if implemented)
- [ ] Re-checked the agent's `system_prompt` is being passed (no caps in warnings)

## Run report

<!-- Paste the full Report.warnings list, the per_metric scores, AND ideally
the JSON file (or attach it) so we can re-analyze the per-turn audits. -->
