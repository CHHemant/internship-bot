"""
Metrics hooks for the orchestrator pipeline.
Import and call these in orchestrator/pipeline.py at the right points.

Why separate file instead of patching pipeline.py:
  - Pipeline logic stays clean
  - Metrics can be disabled by not importing this module
  - Easier to test pipeline without Prometheus dependency
"""

from __future__ import annotations
import time
from contextlib import asynccontextmanager

from models.schemas import VerificationResult


def record_listing_discovered(platform: str, country: str, count: int = 1) -> None:
    try:
        from infra.metrics import listings_discovered
        listings_discovered.labels(platform=platform, country=country).inc(count)
    except ImportError:
        pass


def record_application_generated(country: str, portal: str) -> None:
    try:
        from infra.metrics import applications_generated
        applications_generated.labels(country=country, portal=portal).inc()
    except ImportError:
        pass


def record_application_submitted(country: str, portal: str) -> None:
    try:
        from infra.metrics import applications_submitted
        applications_submitted.labels(country=country, portal=portal).inc()
    except ImportError:
        pass


def record_application_skipped(reason: str) -> None:
    try:
        from infra.metrics import applications_skipped
        applications_skipped.labels(reason=reason).inc()
    except ImportError:
        pass


def record_verification_result(result: VerificationResult, country: str, retries: int) -> None:
    try:
        from infra.metrics import record_verification
        record_verification(result, country, retries)
    except ImportError:
        pass


def record_form_fill(portal: str, duration_sec: float, success: bool, reason: str = "") -> None:
    try:
        from infra.metrics import (
            form_fill_duration, form_fill_success, form_fill_failure
        )
        form_fill_duration.labels(portal=portal).observe(duration_sec)
        if success:
            form_fill_success.labels(portal=portal).inc()
        else:
            form_fill_failure.labels(portal=portal, reason=reason or "unknown").inc()
    except ImportError:
        pass


def record_submission(portal: str, country: str, success: bool, reason: str = "") -> None:
    try:
        from infra.metrics import submission_success, submission_failure
        if success:
            submission_success.labels(portal=portal, country=country).inc()
        else:
            submission_failure.labels(portal=portal, reason=reason or "unknown").inc()
    except ImportError:
        pass


def record_captcha(portal: str) -> None:
    try:
        from infra.metrics import captcha_encounters
        captcha_encounters.labels(portal=portal).inc()
    except ImportError:
        pass


def set_human_queue_size(size: int) -> None:
    try:
        from infra.metrics import human_queue_size
        human_queue_size.set(size)
    except ImportError:
        pass


def record_response(response_type: str, days: float, country: str) -> None:
    try:
        from infra.metrics import record_response as _rr
        _rr(response_type, days, country)
    except ImportError:
        pass


@asynccontextmanager
async def track_in_flight():
    """Context manager: increment in-flight gauge on enter, decrement on exit."""
    try:
        from infra.metrics import applications_in_flight
        applications_in_flight.inc()
        try:
            yield
        finally:
            applications_in_flight.dec()
    except ImportError:
        yield
