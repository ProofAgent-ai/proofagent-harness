---
name: Bug report
about: A run failed, scores look wrong, or the harness behaved unexpectedly
title: "[bug] "
labels: bug
---

## What happened

<!-- One-paragraph description. Include the failure mode (crash / wrong score /
hang / etc.) and what you expected to see instead. -->

## Reproduce

```bash
# The exact command you ran:
python examples/01_quickstart.py --turns N --consensus delphi --llm <model>
```

```python
# Or: the minimal Python snippet that reproduces:
from proofagent_harness import Harness, AgentContext
report = Harness(...).evaluate(my_agent, ...)
```

## Output / scorecard / traceback

```
<paste the full output, including any traceback or the scorecard JSON>
```

## Environment

- OS: <macOS / Linux / Windows + version>
- Python: <`python --version`>
- proofagent-harness: <`pip show proofagent-harness | grep Version`>
- LLM provider + model: <e.g. `--llm claude-sonnet-4-6` via Anthropic SDK X.Y.Z>

## Anything else

<!-- Workarounds you tried, related runs that DID work, screenshots of
weird scorecards, etc. -->
