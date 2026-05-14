"""
Main pipeline for a single job listing.

Steps: analyze JD → generate resume + cover letter in parallel → verify
(loop up to 3 times if it fails) → route to portal → fill form → submit.

Nothing here is clever. It's just running agents in the right order and
handling the stuff that goes wrong.

Things that go wrong a lot:
  - JD analyzer returns garbage JSON sometimes → retried automatically
  - Verification hits max retries on listings where our skills genuinely
    don't match well enough. That's correct behavior, not a bug.
  - DAAD form filler times out intermittently. No idea why. Retry fixes it.
  - Playwright sessions occasionally leak if the worker crashes mid-fill.
    The SecureFileManager cleanup catches temp files though.

dry_run=True by default in prefs. Don't flip that until you've manually
reviewed at least 3 applications end-to-end.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import structlog

from agents.jd_analyzer.agent import JDAnalyzerAgent
from agents.resume_customizer.agent import ResumeCustomizerAgent
from agents.cover_letter.agent import CoverLetterAgent
from agents.verifier import VerificationSuite
from agents.router.agent import ApplicationRouterAgent
from agents.form_filler.agent import FormFillerAgent
from agents.submission.agent import SubmissionAgent
from security.vault import AuditLog, SecureFileManager
from models.schemas import ApplicationRecord, ApplicationStatus, JobListing, MasterResume, UserPrefs

MAX_VERIFY_RETRIES = 3

log = structlog.get_logger()


async def run_application(
    listing: JobListing,
    master: MasterResume,
    prefs: UserPrefs,
    db=None,
    error_bus=None,
) -> ApplicationRecord:

    app_id = f"{listing.company[:20]}-{listing.id[:12]}".replace(" ", "_")
    record = ApplicationRecord(
        id=app_id,
        listing=listing,
        resume_path="",
        cover_letter_path="",
        verification=None,
        status=ApplicationStatus.GENERATING,
    )

    AuditLog.write("start", app=app_id, company=listing.company, country=listing.country.value)
    log.info("pipeline_start", app=app_id)

    # step 1: figure out what the JD actually wants
    try:
        jd = await JDAnalyzerAgent(error_bus).run(listing)
    except Exception as e:
        return _fail(record, f"JD analysis blew up: {e}")

    # step 2: generate resume + cover letter, then verify.
    # loop because the first draft often misses a keyword or two.
    notes: list[str] = []
    resume_text = ""
    cover_text = ""

    for attempt in range(MAX_VERIFY_RETRIES):
        record.retry_count = attempt
        record.status = ApplicationStatus.GENERATING

        try:
            # run both generators in parallel — they're independent
            resume_text, cover_text = await asyncio.gather(
                ResumeCustomizerAgent(error_bus).run(master, jd, notes),
                CoverLetterAgent(error_bus).run(master, jd, notes),
            )
        except Exception as e:
            return _fail(record, f"Generation failed (attempt {attempt + 1}): {e}")

        record.status = ApplicationStatus.VERIFYING
        result = await VerificationSuite(error_bus).run(
            resume_text=resume_text,
            cover_letter_text=cover_text,
            jd_analysis=jd,
            master_resume=master,
            retry_count=attempt,
        )
        record.verification = result

        if result.overall_passed:
            log.info("verification_passed", app=app_id, ats=result.ats.score, attempt=attempt)
            break

        notes = result.improvement_notes
        log.info("verification_retry", app=app_id, attempt=attempt, ats=result.ats.score)

        if attempt == MAX_VERIFY_RETRIES - 1:
            # hit the ceiling — put it in human queue for manual review
            log.warning("max_retries_hit", app=app_id)
            record.status = ApplicationStatus.PENDING_HUMAN
            await _save(record, db)
            return record

    # step 3: auto-generate any extra docs this portal needs
    # (DAAD needs a funding statement, Euraxess wants Europass CV)
    extra_docs = await _make_extra_docs(listing, master, jd, app_id)

    # step 4: figure out which portal this is and build the submission package
    try:
        pkg = await ApplicationRouterAgent(error_bus).run(
            listing=listing,
            resume_text=resume_text,
            cover_text=cover_text,
            prefs=prefs,
            master=master,
            extra_docs_paths=extra_docs,
        )
    except Exception as e:
        return _fail(record, f"Routing failed: {e}")

    # step 5: fill the form and (maybe) submit
    # everything inside SecureFileManager so temp files get wiped on exit
    async with SecureFileManager() as sfm:
        pkg.resume_path = sfm.create_temp(resume_text, f"resume_{app_id}.txt")
        pkg.cover_letter_path = sfm.create_temp(cover_text, f"cover_{app_id}.txt")
        record.resume_path = str(pkg.resume_path)
        record.cover_letter_path = str(pkg.cover_letter_path)

        if prefs.dry_run:
            log.info("dry_run_skip_submit", app=app_id)
            record.status = ApplicationStatus.QUEUED
            AuditLog.write("dry_run", app=app_id)
        else:
            filled = None
            try:
                filled = await FormFillerAgent(error_bus).run(pkg)
            except Exception as e:
                return _fail(record, f"Form fill failed: {e}")

            if filled["status"] == "ready_to_submit":
                try:
                    conf = await SubmissionAgent(error_bus).run(filled, listing, db)
                    record.confirmation_id = conf.confirmation_id
                    record.submitted_at = datetime.now(timezone.utc)
                    record.status = ApplicationStatus.SUBMITTED
                    AuditLog.write("submitted", app=app_id, confirmation=conf.confirmation_id)
                except Exception as e:
                    return _fail(record, f"Submission failed: {e}")
            elif filled["status"] == "pending_human":
                record.status = ApplicationStatus.PENDING_HUMAN
            else:
                return _fail(record, f"Filler returned unexpected status: {filled['status']}")

    await _save(record, db)
    log.info("pipeline_done", app=app_id, status=record.status)
    return record


async def _make_extra_docs(listing, master, jd, app_id: str) -> dict[str, Path]:
    """Generate DAAD funding statement or Europass CV if the portal needs them."""
    from agents.router.portal_configs import detect_portal, PortalType
    from agents.doc_generators.special_docs import DAADFundingStatementAgent, EuropassCVGenerator

    portal = detect_portal(str(listing.url))
    out = Path(f"/tmp/internship_bot_secure/docs_{app_id}")
    docs = {}

    if portal.type == PortalType.DAAD:
        try:
            path = out / "funding_statement.txt"
            out.mkdir(parents=True, exist_ok=True, mode=0o700)
            await DAADFundingStatementAgent().run(master, jd, path)
            docs["funding_statement"] = path
        except Exception as e:
            log.warning("daad_funding_failed", err=str(e))

    if portal.type == PortalType.EURAXESS:
        try:
            pdf = await EuropassCVGenerator().run(master, out)
            docs["europass_cv"] = pdf
        except Exception as e:
            log.warning("europass_cv_failed", err=str(e))

    return docs


async def _save(record: ApplicationRecord, db) -> None:
    if db is None:
        return
    from infra.db.models import ApplicationRecordORM
    try:
        orm = ApplicationRecordORM(
            id=record.id,
            listing_id=record.listing.id,
            listing_title=record.listing.title,
            listing_company=record.listing.company,
            listing_country=record.listing.country.value,
            listing_portal=record.listing.portal,
            listing_url=str(record.listing.url),
            listing_deadline=record.listing.deadline,
            resume_path=record.resume_path,
            cover_letter_path=record.cover_letter_path,
            ats_score=record.verification.ats.score if record.verification else 0.0,
            quality_score=record.verification.quality.score if record.verification else 0.0,
            required_coverage=record.verification.ats.required_coverage if record.verification else 0.0,
            verification_passed=record.verification.overall_passed if record.verification else False,
            retry_count=record.retry_count,
            status=record.status.value,
            confirmation_id=record.confirmation_id,
            submitted_at=record.submitted_at,
            error_log=record.error_log,
        )
        async with db() as session:
            await session.merge(orm)
    except Exception as e:
        # don't crash the pipeline over a DB write failure — log and move on
        log.error("db_save_failed", app=record.id, err=str(e))


def _fail(record: ApplicationRecord, msg: str) -> ApplicationRecord:
    record.status = ApplicationStatus.ERROR
    record.error_log.append(msg)
    AuditLog.write("error", app=record.id, msg=msg[:200])
    log.error("pipeline_error", app=record.id, msg=msg)
    return record
