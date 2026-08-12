"""The pre-run banner must name the backend the run will ACTUALLY reach.

It used to print "app.proofagent.ai" unconditionally. A run pointed at an on-prem or air-gapped
backend with PROOFAGENT_API_BASE_URL was therefore told, on screen, that its evidence was going to
the vendor's cloud. For anyone whose reason for running locally is that the data cannot leave, the
destination is the one field in that table it is unacceptable to be wrong about.

Found while confirming where a ten-agent local sweep had sent its data: every run's banner read
"→ app.proofagent.ai" while every upload line read "→ http://localhost:8000".
"""

from __future__ import annotations

import pytest

from proofagent_harness.cli import _upload_target
from proofagent_harness.governance import DEFAULT_API_BASE_URL


def test_it_reports_the_cloud_when_nothing_is_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROOFAGENT_API_BASE_URL", raising=False)
    assert _upload_target() == DEFAULT_API_BASE_URL.split("://", 1)[-1]


@pytest.mark.parametrize(
    ("env", "shown"),
    [
        # The regression: a local backend must not read as the vendor's cloud.
        ("http://localhost:8000", "localhost:8000"),
        # The port is kept — :8000 vs :5173 is exactly what a local operator needs to distinguish.
        ("http://localhost:5173", "localhost:5173"),
        ("https://governance.acme.internal", "governance.acme.internal"),
        # A trailing slash is a normal way to set this and must not leak into the display.
        ("https://governance.acme.internal/", "governance.acme.internal"),
        ("  http://127.0.0.1:8000  ", "127.0.0.1:8000"),
    ],
)
def test_it_reports_the_overridden_backend(
    monkeypatch: pytest.MonkeyPatch, env: str, shown: str,
) -> None:
    monkeypatch.setenv("PROOFAGENT_API_BASE_URL", env)
    assert _upload_target() == shown


def test_an_overridden_backend_never_reads_as_the_vendor_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stated as the property rather than a literal, so it still holds if the default host changes."""
    monkeypatch.setenv("PROOFAGENT_API_BASE_URL", "http://localhost:8000")
    assert DEFAULT_API_BASE_URL.split("://", 1)[-1] not in _upload_target()


def test_the_banner_lines_use_the_helper_rather_than_a_literal() -> None:
    """Both `run` and `artifact` printed the same hardcoded host. Pinning this stops one of them
    drifting back while the other stays fixed."""
    import inspect

    from proofagent_harness import cli

    src = inspect.getsource(cli)
    assert '"yes  →  app.proofagent.ai"' not in src, (
        "a banner is hardcoding the cloud host instead of calling _upload_target()")
    assert src.count('f"yes  →  {_upload_target()}"') >= 2
