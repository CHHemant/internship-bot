"""
Metrics — Prometheus instrumentation for the entire pipeline.

Metric categories:
  1. Pipeline throughput  — listings discovered, apps generated, submitted
  2. Verification quality — ATS scores, retry rates, hallucination flags
  3. Portal performance   — success/fail per portal, latency
  4. Agent latency        — LLM call duration, form fill duration
  5. Error rates          — per severity, per agent
  6. Scraper health       — listings found per platform, proxy ban rate
  7. Business outcomes    — response rate, interview rate, offer rate

Exposed at: GET /metrics  (scraped by Prometheus every 15s)
"""

from __future__ import annotations
import time
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import Callable

from prometheus_client import (
    Counter, Gauge, Histogram, Summary,
    CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
    multiprocess, make_asgi_app,
)

# ── Registry ──────────────────────────────────────────────────────────────────
# Use default registry — works with uvicorn multiprocess mode too
REGISTRY = CollectorRegistry()

# ═══════════════════════════════════════════════════════════════════════════════
# 1. Pipeline throughput
# ═══════════════════════════════════════════════════════════════════════════════

listings_discovered = Counter(
    "bot_listings_discovered_total",
    "Total job listings discovered by scraper",
    ["platform", "country"],
)

applications_generated = Counter(
    "bot_applications_generated_total",
    "Total applications generated (resume + cover letter pairs)",
    ["country", "portal"],
)

applications_submitted = Counter(
    "bot_applications_submitted_total",
    "Total applications successfully submitted",
    ["country", "portal"],
)

applications_skipped = Counter(
    "bot_applications_skipped_total",
    "Listings skipped (low ATS fit, expired, work-auth fail)",
    ["reason"],
)

applications_in_flight = Gauge(
    "bot_applications_in_flight",
    "Applications currently being processed",
)

human_queue_size = Gauge(
    "bot_human_queue_size",
    "Number of applications pending human review",
)

# ═══════════════════════════════════════════════════════════════════════════════
# 2. Verification quality
# ═══════════════════════════════════════════════════════════════════════════════

ats_score_histogram = Histogram(
    "bot_ats_score",
    "ATS score distribution across all generated resumes",
    buckets=[30, 40, 50, 60, 65, 70, 75, 80, 85, 90, 95, 100],
)

ats_required_coverage = Histogram(
    "bot_ats_required_coverage",
    "Required keyword coverage (0–1) per resume",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

quality_score_histogram = Histogram(
    "bot_quality_score",
    "Quality review score distribution",
    buckets=[3, 4, 5, 6, 7, 7.5, 8, 8.5, 9, 9.5, 10],
)

verification_pass_total = Counter(
    "bot_verification_pass_total",
    "Verifications that passed both ATS + quality",
    ["country"],
)

verification_fail_total = Counter(
    "bot_verification_fail_total",
    "Verifications that failed",
    ["layer", "country"],  # layer: "ats" | "quality"
)

verification_retries = Histogram(
    "bot_verification_retries",
    "Number of retries before verification passed",
    buckets=[0, 1, 2, 3],
)

hallucinations_detected = Counter(
    "bot_hallucinations_detected_total",
    "Hallucinated claims caught by quality reviewer",
)

# ═══════════════════════════════════════════════════════════════════════════════
# 3. Portal performance
# ═══════════════════════════════════════════════════════════════════════════════

form_fill_duration = Histogram(
    "bot_form_fill_duration_seconds",
    "Time to fill application form per portal",
    ["portal"],
    buckets=[5, 10, 20, 30, 45, 60, 90, 120, 180, 300],
)

form_fill_success = Counter(
    "bot_form_fill_success_total",
    "Successful form fills",
    ["portal"],
)

form_fill_failure = Counter(
    "bot_form_fill_failure_total",
    "Failed form fills",
    ["portal", "reason"],  # reason: captcha | timeout | selector_not_found | auth
)

captcha_encounters = Counter(
    "bot_captcha_encounters_total",
    "CAPTCHA detections (always results in human escalation)",
    ["portal"],
)

submission_success = Counter(
    "bot_submission_success_total",
    "Successful submissions with confirmation ID",
    ["portal", "country"],
)

submission_failure = Counter(
    "bot_submission_failure_total",
    "Failed submissions",
    ["portal", "reason"],
)

# ═══════════════════════════════════════════════════════════════════════════════
# 4. Agent latency
# ═══════════════════════════════════════════════════════════════════════════════

llm_call_duration = Histogram(
    "bot_llm_call_duration_seconds",
    "LLM API call duration per agent",
    ["agent"],
    buckets=[0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60],
)

llm_calls_total = Counter(
    "bot_llm_calls_total",
    "Total LLM API calls",
    ["agent"],
)

llm_tokens_used = Counter(
    "bot_llm_tokens_total",
    "Estimated tokens used (input + output)",
    ["agent"],
)

resume_generation_duration = Histogram(
    "bot_resume_generation_duration_seconds",
    "Resume customizer end-to-end duration",
    buckets=[2, 5, 10, 15, 20, 30],
)

cover_letter_generation_duration = Histogram(
    "bot_cover_letter_generation_duration_seconds",
    "Cover letter agent end-to-end duration",
    buckets=[2, 5, 10, 15, 20, 30],
)

jd_analysis_duration = Histogram(
    "bot_jd_analysis_duration_seconds",
    "JD analyzer duration per listing",
    buckets=[1, 2, 3, 5, 8, 12],
)

# ═══════════════════════════════════════════════════════════════════════════════
# 5. Error rates
# ═══════════════════════════════════════════════════════════════════════════════

pipeline_errors = Counter(
    "bot_pipeline_errors_total",
    "Pipeline errors by severity and agent",
    ["severity", "agent"],
)

error_handler_actions = Counter(
    "bot_error_handler_actions_total",
    "Actions taken by error handler",
    ["action"],  # retry | skip | human_queue | halt
)

# ═══════════════════════════════════════════════════════════════════════════════
# 6. Scraper health
# ═══════════════════════════════════════════════════════════════════════════════

scrape_duration = Histogram(
    "bot_scrape_duration_seconds",
    "Time to scrape one page from a platform",
    ["platform"],
    buckets=[1, 2, 3, 5, 8, 12, 20, 30],
)

scrape_listings_per_page = Histogram(
    "bot_scrape_listings_per_page",
    "Listings found per scraped page",
    ["platform"],
    buckets=[0, 2, 5, 10, 15, 20, 30],
)

proxy_bans = Counter(
    "bot_proxy_bans_total",
    "Number of proxy entries banned",
)

proxy_pool_size = Gauge(
    "bot_proxy_pool_available",
    "Available (non-banned) proxies in pool",
)

playwright_fallbacks = Counter(
    "bot_playwright_fallbacks_total",
    "Times httpx failed and Playwright was used as fallback",
    ["platform"],
)

# ═══════════════════════════════════════════════════════════════════════════════
# 7. Business outcomes
# ═══════════════════════════════════════════════════════════════════════════════

application_status_gauge = Gauge(
    "bot_application_status_count",
    "Current count of applications in each status",
    ["status"],
)

response_received = Counter(
    "bot_responses_received_total",
    "Application responses received",
    ["type"],  # interview | offer | rejection | info_request
)

response_rate_gauge = Gauge(
    "bot_response_rate",
    "Rolling response rate (responded / submitted)",
    ["country"],
)

days_to_response = Histogram(
    "bot_days_to_response",
    "Days from submission to first response",
    buckets=[1, 3, 5, 7, 10, 14, 21, 30, 45],
)


# ═══════════════════════════════════════════════════════════════════════════════
# Instrumentation helpers
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def track_duration(histogram: Histogram, **labels):
    """Context manager: time a block and observe into a histogram."""
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        if labels:
            histogram.labels(**labels).observe(elapsed)
        else:
            histogram.observe(elapsed)


def track_llm_call(agent_name: str):
    """Decorator: track LLM call duration + count on any async method."""
    def decorator(fn):
        @wraps(fn)
        async def wrapper(self, *args, **kwargs):
            llm_calls_total.labels(agent=agent_name).inc()
            start = time.monotonic()
            try:
                result = await fn(self, *args, **kwargs)
                llm_call_duration.labels(agent=agent_name).observe(
                    time.monotonic() - start
                )
                return result
            except Exception:
                llm_call_duration.labels(agent=agent_name).observe(
                    time.monotonic() - start
                )
                raise
        return wrapper
    return decorator


def record_verification(result, country: str, retries: int) -> None:
    """Called after VerificationSuite.run() — records all verification metrics."""
    ats_score_histogram.observe(result.ats.score)
    ats_required_coverage.observe(result.ats.required_coverage)
    quality_score_histogram.observe(result.quality.score)
    verification_retries.observe(retries)

    if result.overall_passed:
        verification_pass_total.labels(country=country).inc()
    else:
        layer = "quality" if result.ats.passed else "ats"
        verification_fail_total.labels(layer=layer, country=country).inc()

    if result.quality.hallucination_flags:
        hallucinations_detected.inc(len(result.quality.hallucination_flags))


def record_response(response_type: str, days_elapsed: float, country: str) -> None:
    """Called by Response Tracker when a status change is detected."""
    response_received.labels(type=response_type).inc()
    days_to_response.observe(days_elapsed)


def update_status_gauges(status_counts: dict[str, int]) -> None:
    """Called periodically to update current application status distribution."""
    for status, count in status_counts.items():
        application_status_gauge.labels(status=status).set(count)
