"""
Response Tracker — monitors submitted applications for status changes.

Two sources:
  1. Email inbox (IMAP) — classifies recruiter replies using LLM
  2. Portal status APIs — polls LinkedIn/Handshake where available

Runs as a scheduled background job every 6 hours.
Writes status transitions to ApplicationRecord in DB.
Emits follow-up reminders at +7 days if no response.
"""

from __future__ import annotations
import asyncio
import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from agents.base_agent import BaseAgent
from models.schemas import ApplicationRecord, ApplicationStatus

log = structlog.get_logger()

# ─── Email classification ─────────────────────────────────────────────────────

EMAIL_CLASSIFY_PROMPT = """
You are classifying a recruiter email about a job application.
Output ONLY one of these labels (no quotes, no explanation):

  INTERVIEW_INVITE  - they want to schedule an interview
  REJECTION         - application was not successful
  INFO_REQUEST      - they need more information or documents
  OFFER             - job offer received
  ACKNOWLEDGEMENT   - auto-reply or receipt confirmation only
  OTHER             - anything else

Email subject: {subject}
Email body (first 500 chars): {body}
"""

# Patterns to detect application-related emails
APPLICATION_KEYWORDS = [
    "application", "internship", "position", "candidacy",
    "bewerbung", "praktikum",        # German
    "candidature", "stage",          # French
    "sollicitatie",                   # Dutch
]


class ResponseTracker(BaseAgent):

    def __init__(self, error_bus=None):
        super().__init__(error_bus)
        self._imap: imaplib.IMAP4_SSL | None = None

    async def run_cycle(self, records: list[ApplicationRecord], db=None) -> list[ApplicationRecord]:
        """
        One full tracking cycle. Call every 6 hours.
        Updates each record's status in-place and returns updated list.
        """
        submitted = [r for r in records if r.status in (
            ApplicationStatus.SUBMITTED, ApplicationStatus.VIEWED
        )]

        if not submitted:
            self.log.info("no_submitted_apps_to_track")
            return records

        self.log.info("tracking_cycle_start", count=len(submitted))

        # Run email scan and follow-up check concurrently
        email_updates = await self._scan_inbox(submitted)
        records = self._apply_updates(records, email_updates)
        records = self._check_followups(records)

        self.log.info("tracking_cycle_complete", updates=len(email_updates))
        return records

    # ─── Email scanning ───────────────────────────────────────────────────────

    async def _scan_inbox(self, records: list[ApplicationRecord]) -> dict[str, ApplicationStatus]:
        """Returns dict of application_id → new status from email signals."""
        from config.settings import settings
        updates: dict[str, ApplicationStatus] = {}

        try:
            # Run IMAP in thread (blocking library)
            emails = await asyncio.to_thread(
                self._fetch_recent_emails,
                settings.IMAP_HOST,
                settings.IMAP_PORT,
                settings.IMAP_USER,
                settings.IMAP_PASS,
            )
        except Exception as e:
            self.log.error("imap_fetch_failed", error=str(e))
            return updates

        # Build lookup: company name → application record
        company_map = {r.listing.company.lower(): r for r in records}

        for msg in emails:
            subject = msg.get("subject", "")
            body    = msg.get("body", "")[:500]
            from_addr = msg.get("from", "").lower()

            # Filter: only emails plausibly about an application
            combined = (subject + body).lower()
            if not any(kw in combined for kw in APPLICATION_KEYWORDS):
                continue

            # Match to a company in our records
            matched_record = None
            for company, rec in company_map.items():
                if company in combined or company in from_addr:
                    matched_record = rec
                    break

            if not matched_record:
                continue

            # Classify with LLM
            label = await self._classify_email(subject, body)
            new_status = self._label_to_status(label)

            if new_status and new_status != matched_record.status:
                self.log.info(
                    "status_update",
                    application=matched_record.id,
                    old=matched_record.status,
                    new=new_status,
                    label=label,
                )
                updates[matched_record.id] = new_status

        return updates

    def _fetch_recent_emails(
        self, host: str, port: int, user: str, password: str, days: int = 3
    ) -> list[dict[str, str]]:
        """Blocking IMAP fetch — run via asyncio.to_thread."""
        messages = []
        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

        with imaplib.IMAP4_SSL(host, port) as mail:
            mail.login(user, password)
            mail.select("inbox")
            _, ids = mail.search(None, f'(SINCE "{since_date}")')

            for msg_id in (ids[0] or b"").split()[-50:]:  # last 50 emails
                try:
                    _, data = mail.fetch(msg_id, "(RFC822)")
                    raw = data[0][1]
                    msg = email.message_from_bytes(raw)
                    body = self._extract_body(msg)
                    messages.append({
                        "subject": str(msg.get("Subject", "")),
                        "from":    str(msg.get("From", "")),
                        "body":    body,
                    })
                except Exception as e:
                    self.log.warning("email_parse_error", error=str(e))

        return messages

    @staticmethod
    def _extract_body(msg) -> str:
        """Extract plain text from email message."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        # Strip HTML tags if any slipped through
        body = re.sub(r"<[^>]+>", " ", body)
        return body[:1000]

    async def _classify_email(self, subject: str, body: str) -> str:
        """LLM single-label classification. Returns label string."""
        try:
            prompt = EMAIL_CLASSIFY_PROMPT.format(subject=subject, body=body)
            result = await self._llm(
                system="You classify recruiter emails. Output ONLY the label.",
                user=prompt,
                max_tokens=10,
            )
            return result.strip().upper()
        except Exception as e:
            self.log.warning("email_classify_failed", error=str(e))
            return "OTHER"

    @staticmethod
    def _label_to_status(label: str) -> ApplicationStatus | None:
        mapping = {
            "INTERVIEW_INVITE": ApplicationStatus.INTERVIEW,
            "REJECTION":        ApplicationStatus.REJECTED,
            "OFFER":            ApplicationStatus.OFFER,
            "INFO_REQUEST":     ApplicationStatus.VIEWED,   # they saw it
        }
        return mapping.get(label)

    # ─── Apply updates and follow-up checks ──────────────────────────────────

    @staticmethod
    def _apply_updates(
        records: list[ApplicationRecord],
        updates: dict[str, ApplicationStatus],
    ) -> list[ApplicationRecord]:
        for r in records:
            if r.id in updates:
                r.status = updates[r.id]
                r.response_received_at = datetime.now(timezone.utc)
                r.last_updated = datetime.now(timezone.utc)
        return records

    @staticmethod
    def _check_followups(records: list[ApplicationRecord]) -> list[ApplicationRecord]:
        """Flag apps with no response after 7 days for follow-up reminder."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for r in records:
            if (
                r.status == ApplicationStatus.SUBMITTED
                and r.submitted_at
                and r.submitted_at < cutoff
                and not r.response_received_at
            ):
                log.info(
                    "followup_reminder",
                    application=r.id,
                    company=r.listing.company,
                    days_since_submit=(datetime.now(timezone.utc) - r.submitted_at).days,
                )
                # In production: emit follow-up event / send user email
        return records
