"""
Celery Task Queue — replaces bare asyncio.gather in orchestrator.

Why Celery over asyncio.gather:
  - Tasks survive process crashes (Redis-backed)
  - Retry state persists across restarts
  - Rate limiting per task type
  - Real-time monitoring via Flower
  - Tasks can be paused/cancelled without killing everything

Workers:
  - discovery_worker   : job scraping (low concurrency, polite)
  - generation_worker  : LLM resume/cover letter (medium concurrency)
  - apply_worker       : Playwright form fill + submit (low, browser-heavy)
  - tracker_worker     : scheduled email + portal polling

Run workers:
  celery -A infra.celery_app worker -Q discovery -c 2 --loglevel=info
  celery -A infra.celery_app worker -Q generation -c 4 --loglevel=info
  celery -A infra.celery_app worker -Q apply -c 2 --loglevel=info
  celery -A infra.celery_app beat --loglevel=info   # scheduler for tracker
"""

from __future__ import annotations
import asyncio
from functools import wraps
from typing import Any

from celery import Celery
from celery.schedules import crontab
import structlog

log = structlog.get_logger()


def _make_app(redis_url: str) -> Celery:
    app = Celery("internship_bot", broker=redis_url, backend=redis_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        task_track_started=True,
        task_acks_late=True,              # ack only after task completes
        worker_prefetch_multiplier=1,     # one task at a time per worker
        task_reject_on_worker_lost=True,  # re-queue if worker crashes mid-task
        task_routes={
            "infra.celery_app.scrape_platform":      {"queue": "discovery"},
            "infra.celery_app.process_listing":      {"queue": "generation"},
            "infra.celery_app.fill_and_submit":      {"queue": "apply"},
            "infra.celery_app.run_tracker_cycle":    {"queue": "tracker"},
            "infra.celery_app.run_analytics_cycle":  {"queue": "tracker"},
        },
        beat_schedule={
            "tracker-every-6h": {
                "task": "infra.celery_app.run_tracker_cycle",
                "schedule": crontab(minute=0, hour="*/6"),
            },
            "analytics-daily": {
                "task": "infra.celery_app.run_analytics_cycle",
                "schedule": crontab(minute=0, hour=8),
            },
        },
    )
    return app


def _get_redis_url() -> str:
    try:
        from config.settings import settings
        return settings.REDIS_URL
    except Exception:
        return "redis://localhost:6379/0"


celery_app = _make_app(_get_redis_url())


# ── Async bridge ──────────────────────────────────────────────────────────────

def async_task(fn):
    """Decorator: lets async functions run as Celery tasks."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════════
# Task definitions
# ═══════════════════════════════════════════════════════════════════════════════

@celery_app.task(
    name="infra.celery_app.scrape_platform",
    bind=True,
    max_retries=3,
    default_retry_delay=60,     # 1 min base, doubles each retry
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    rate_limit="10/m",          # 10 scrape tasks per minute across all workers
)
@async_task
async def scrape_platform(
    self,
    platform_id: str,
    query: str,
    country: str,
    master_resume_json: dict,
    prefs_json: dict,
) -> list[dict]:
    """Scrape one platform for internship listings. Returns serialized JobListing list."""
    from agents.job_discovery.platforms import PLATFORMS
    from agents.job_discovery.scraper import UniversalScraper
    from models.schemas import Country, MasterResume, UserPrefs

    platform = next((p for p in PLATFORMS if p.id == platform_id), None)
    if not platform:
        raise ValueError(f"Unknown platform: {platform_id}")

    scraper = UniversalScraper(platform)
    listings = await scraper.scrape(query, Country(country))
    return [l.model_dump(mode="json") for l in listings]


@celery_app.task(
    name="infra.celery_app.process_listing",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    time_limit=300,             # 5 min hard limit per listing
    soft_time_limit=240,
)
@async_task
async def process_listing(
    self,
    listing_json: dict,
    master_resume_json: dict,
    prefs_json: dict,
) -> dict:
    """
    Full pipeline for one listing: analyze → generate → verify → route.
    Returns ApplicationRecord as dict (serializable).
    """
    from models.schemas import JobListing, MasterResume, UserPrefs
    from orchestrator.pipeline import run_application

    listing = JobListing(**listing_json)
    master  = MasterResume(**master_resume_json)
    prefs   = UserPrefs(**prefs_json)

    record = await run_application(listing, master, prefs)
    return record.model_dump(mode="json")


@celery_app.task(
    name="infra.celery_app.fill_and_submit",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    time_limit=600,             # 10 min — Playwright can be slow
    soft_time_limit=540,
    rate_limit="5/m",           # max 5 browser sessions launching per minute
)
@async_task
async def fill_and_submit(self, application_record_json: dict, prefs_json: dict) -> dict:
    """
    Browser automation for one application.
    Only called if process_listing succeeded and DRY_RUN=false.
    """
    from models.schemas import ApplicationRecord, UserPrefs
    from agents.form_filler.agent import FormFillerAgent
    from agents.submission.agent import SubmissionAgent
    from agents.router.agent import ApplicationRouterAgent

    record = ApplicationRecord(**application_record_json)
    prefs  = UserPrefs(**prefs_json)

    if prefs.dry_run:
        log.info("dry_run_fill_skip", application=record.id)
        return record.model_dump(mode="json")

    # Re-build submission package from record
    router = ApplicationRouterAgent()
    pkg = await router.run(
        listing=record.listing,
        resume_text="",    # loaded from encrypted path in full impl
        cover_text="",
        prefs=prefs,
    )

    filler = FormFillerAgent()
    filled_state = await filler.run(pkg)

    if filled_state["status"] == "ready_to_submit":
        submitter = SubmissionAgent()
        result = await submitter.run(filled_state, record.listing)
        record.confirmation_id = result.confirmation_id
        record.submitted_at = result.submitted_at
        from models.schemas import ApplicationStatus
        record.status = ApplicationStatus.SUBMITTED

    return record.model_dump(mode="json")


@celery_app.task(
    name="infra.celery_app.run_tracker_cycle",
    bind=True,
    max_retries=1,
    time_limit=3600,
)
@async_task
async def run_tracker_cycle(self) -> dict:
    """Scheduled: scan inbox + portal APIs for status updates."""
    from agents.tracker.agent import ResponseTracker
    tracker = ResponseTracker()
    # In production: load records from DB
    await tracker.run_cycle(records=[], db=None)
    return {"status": "done"}


@celery_app.task(
    name="infra.celery_app.run_analytics_cycle",
    bind=True,
    max_retries=1,
    time_limit=600,
)
@async_task
async def run_analytics_cycle(self) -> dict:
    """Scheduled daily: compute analytics + update preference weights."""
    from agents.analytics.agent import AnalyticsAgent
    from models.schemas import UserPrefs
    analytics = AnalyticsAgent()
    # In production: load records + prefs from DB
    log.info("analytics_cycle_triggered")
    return {"status": "done"}


# ── Dispatcher: fan-out listings to Celery ───────────────────────────────────

class PipelineDispatcher:
    """
    Called by CLI or API. Fans out scraping + processing tasks to Celery.
    Non-blocking — returns immediately, tasks run in workers.
    """

    @staticmethod
    def dispatch_discovery(
        platform_ids: list[str],
        queries: list[str],
        countries: list[str],
        master_resume_json: dict,
        prefs_json: dict,
    ) -> list[str]:
        """Submit all scrape tasks. Returns list of Celery task IDs."""
        task_ids = []
        for platform_id in platform_ids:
            for query in queries:
                for country in countries:
                    result = scrape_platform.apply_async(
                        args=[platform_id, query, country, master_resume_json, prefs_json],
                        countdown=0,
                    )
                    task_ids.append(result.id)
        log.info("discovery_dispatched", tasks=len(task_ids))
        return task_ids

    @staticmethod
    def dispatch_listing(listing_json: dict, master_json: dict, prefs_json: dict) -> str:
        """Submit one listing for full pipeline processing. Returns task ID."""
        result = process_listing.apply_async(
            args=[listing_json, master_json, prefs_json],
        )
        return result.id

    @staticmethod
    def dispatch_submission(record_json: dict, prefs_json: dict) -> str:
        result = fill_and_submit.apply_async(
            args=[record_json, prefs_json],
            countdown=5,  # brief delay to avoid thundering herd
        )
        return result.id
