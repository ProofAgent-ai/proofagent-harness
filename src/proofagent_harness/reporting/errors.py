"""Custom exceptions for the live reporting subsystem.

These are NEVER raised into the evaluation pipeline. The reporter catches
them internally and falls back to local cache. They exist only for tests
and for diagnostic logging.
"""
from __future__ import annotations


class LiveReportingError(Exception):
    """Base class for all reporting failures."""


class ReportingUnavailableError(LiveReportingError):
    """Transient failure: backend unreachable, 5xx, timeout. Will retry, then queue locally."""


class ReportingAuthError(LiveReportingError):
    """Permanent failure: 401 / 403 from the backend. Disable reporting for the session."""


class ReportingQuotaError(LiveReportingError):
    """Backend returned 429 — quota exceeded. Queue locally for later."""
