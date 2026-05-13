## What this changes

<!-- One-paragraph summary. Cite the issue / discussion this addresses if any. -->

## Pipeline stage(s) touched

- [ ] Trap (new family or modification to existing trap)
- [ ] Skill (planner / conductor / juror persona / scoring rubric)
- [ ] Schema (`schemas.py`)
- [ ] Conductor (defect detection, agent invocation)
- [ ] Juror (audit protocol, lens, sharpener)
- [ ] Reporter (warnings, summary, output)
- [ ] CLI / examples
- [ ] Tests / benchmarks
- [ ] Docs / README

## Tests

- [ ] All existing tests still pass (`pytest tests/`)
- [ ] New tests added for the change (state which file)
- [ ] Calibration benchmark still produces gap ≥ 3.0 (or explained why not)

## Discrimination impact

<!-- If the change affects scoring, run the calibration check before AND after:
     python benchmarks/calibration_check.py --turns 15
     Report both numbers. -->

| | Before | After |
|---|---:|---:|
| Hardened-agent score | | |
| Weak-agent score | | |
| Discrimination gap | | |

## Anything reviewers should know

<!-- Trade-offs you considered, design alternatives you rejected, edge cases
you're unsure about, follow-up work you've punted on. -->
