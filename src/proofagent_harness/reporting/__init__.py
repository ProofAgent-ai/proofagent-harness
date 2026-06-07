"""Live Reporting — opt in evaluation reporting to the ProofAgent dashboard.

The harness reports completed evaluations to the customer's ProofAgent tenant
when `live_reporting=True` is passed to `Harness(...)`. By default the
feature is OFF — no network calls are made, no data leaves the process.

Quick start
-----------
1. Create an evaluation project at https://app.proofagent.ai/projects and
   copy the API key shown once at creation.

2. Export the key::

       export PROOFAGENT_API_KEY="apk_live_eval_..."

3. Enable in code::

       from proofagent_harness import Harness

       harness = Harness(
           llm="groq/llama-3.3-70b-versatile",
           turns=50,
           consensus="debate",
           live_reporting=True,
       )
       report = harness.evaluate(my_agent, role="...", ...)
       # [report] Live run dashboard: https://app.proofagent.ai/evaluations/<id>
       # [report] sync OK -> https://app.proofagent.ai/evaluations/<id>

If the network is unreachable, the report is queued locally at
``~/.proofagent/pending_reports/<idempotency_key>.json`` and can be flushed
later with ``proofagent reporting sync``. **No evaluation ever fails because
of reporting** — reporting is strictly side car to the evaluation pipeline.

Privacy
-------
Reports are sent over TLS to the customer's own ProofAgent tenant. The data
stays in that tenant. Nothing is shared with third parties.
"""

from proofagent_harness.reporting.errors import (
    LiveReportingError,
    ReportingUnavailableError,
)
from proofagent_harness.reporting.reporter import LiveReporter, ReportingConfig

__all__ = [
    "LiveReporter",
    "ReportingConfig",
    "LiveReportingError",
    "ReportingUnavailableError",
]
