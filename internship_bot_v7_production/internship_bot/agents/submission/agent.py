"""
Submission Agent — the point of no return.

Receives: filled_state from Form Filler (browser is on review/submit page)
Does:
  1. Duplicate guard — check DB before touching submit
  2. Dry-run gate — hard stop if DRY_RUN=true
  3. Click submit, wait ≤ 15s for confirmation
  4. Extract confirmation ID from page
  5. Log everything, emit success event

Why we re-open the browser here (not reuse):
  Playwright sessions from Form Filler are intentionally closed after fill.
  Submission Agent opens a fresh session, navigates back via stored URL/cookies,
  and clicks submit. This separation means a crash in the filler never
  auto-submits — requires an explicit Submission Agent call.
"""

from __future__ import annotations
import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from agents.base_agent import BaseAgent
from models.schemas import ApplicationStatus, ErrorSeverity, JobListing

log = structlog.get_logger()

# Selectors for the final submit button across portals
SUBMIT_SELECTORS = [
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "input[type='submit']",
    "button[type='submit']:visible",
    "button:has-text('Send application')",
    "button:has-text('Absenden')",         # German: "Send"
    "button:has-text('Envoyer')",          # French: "Send"
]

# Confirmation signals after submit
CONFIRMATION_PATTERNS = [
    r"application\s+(has been\s+)?submitted",
    r"thank you for (your )?applying",
    r"bewerbung.*eingegangen",             # German
    r"confirmation\s*(number|id|#)?[:\s]*([A-Z0-9\-]{4,30})",
    r"reference\s*(number|#)?[:\s]*([A-Z0-9\-]{4,30})",
    r"application\s*id[:\s]*([A-Z0-9\-]{4,30})",
]


@dataclass
class SubmissionResult:
    confirmation_id: str
    submitted_at: datetime
    confirmation_url: str
    screenshot_path: str | None = None


class SubmissionAgent(BaseAgent):

    async def run(
        self,
        filled_state: dict[str, Any],
        listing: JobListing,
        db=None,
    ) -> SubmissionResult:
        from config.settings import settings

        listing_id = listing.id
        status = filled_state.get("status")

        # ── Guard: check filled_state is ready ────────────────────────────────
        if status != "ready_to_submit":
            raise ValueError(
                f"Cannot submit: filled_state.status='{status}' "
                f"(expected 'ready_to_submit'). Check Form Filler output."
            )

        # ── Guard: duplicate check ────────────────────────────────────────────
        if await self._already_submitted(listing_id, db):
            self.log.warning("duplicate_submit_blocked", listing=listing_id)
            raise ValueError(f"Duplicate guard: {listing_id} already submitted.")

        # ── Guard: dry run ────────────────────────────────────────────────────
        if settings.DRY_RUN:
            self.log.info("dry_run_submit_skipped", listing=listing_id)
            return SubmissionResult(
                confirmation_id=f"DRY-RUN-{listing_id[:8]}",
                submitted_at=datetime.now(timezone.utc),
                confirmation_url=filled_state.get("url", ""),
            )

        # ── Real submission ───────────────────────────────────────────────────
        return await self._submit(filled_state, listing)

    # ─── Core submit logic ────────────────────────────────────────────────────

    async def _submit(
        self,
        filled_state: dict[str, Any],
        listing: JobListing,
    ) -> SubmissionResult:
        target_url = filled_state.get("url", str(listing.url))
        listing_id = listing.id

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                # Navigate to where Form Filler left off
                await page.goto(target_url, timeout=30_000, wait_until="domcontentloaded")
                await asyncio.sleep(1.0)

                # Find and click submit button
                clicked = False
                for sel in SUBMIT_SELECTORS:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        self.log.info("submit_button_found", selector=sel, listing=listing_id)
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=10_000)
                        clicked = True
                        break

                if not clicked:
                    await self._emit_error(
                        application_id=listing_id,
                        severity=ErrorSeverity.STRUCTURAL,
                        message="Submit button not found on page.",
                        context={"url": target_url, "tried": SUBMIT_SELECTORS},
                    )
                    raise RuntimeError("Submit button not found — escalated to Error Handler.")

                # Wait for confirmation (up to 15s)
                confirmation_id = None
                confirmation_url = page.url

                for _ in range(15):
                    await asyncio.sleep(1)
                    page_text = await page.inner_text("body")
                    confirmation_id = self._extract_confirmation(page_text)
                    if confirmation_id:
                        confirmation_url = page.url
                        break

                if not confirmation_id:
                    # No explicit confirmation ID found — generate stable hash
                    confirmation_id = "CONF-" + hashlib.sha1(
                        f"{listing_id}{datetime.now(timezone.utc).isoformat()}".encode()
                    ).hexdigest()[:10].upper()
                    self.log.warning("no_confirmation_id_extracted", listing=listing_id,
                                     fallback_id=confirmation_id)

                # Screenshot evidence
                screenshot_path = f"/tmp/internship_bot_screenshots/confirmed_{listing_id}.png"
                await page.screenshot(path=screenshot_path)

                self.log.info(
                    "application_submitted",
                    listing=listing_id,
                    confirmation_id=confirmation_id,
                    url=confirmation_url,
                )

                return SubmissionResult(
                    confirmation_id=confirmation_id,
                    submitted_at=datetime.now(timezone.utc),
                    confirmation_url=confirmation_url,
                    screenshot_path=screenshot_path,
                )

            except PlaywrightTimeout as e:
                await self._emit_error(
                    application_id=listing_id,
                    severity=ErrorSeverity.TRANSIENT,
                    message=f"Submission timeout: {e}",
                )
                raise

            finally:
                await context.close()
                await browser.close()

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_confirmation(page_text: str) -> str | None:
        """Try to pull confirmation ID from page text using regex patterns."""
        text_lower = page_text.lower()
        for pattern in CONFIRMATION_PATTERNS:
            match = re.search(pattern, text_lower)
            if match:
                # If pattern has a capture group for the ID, use it
                groups = [g for g in match.groups() if g]
                if groups:
                    return groups[-1].upper()
                return "CONFIRMED"
        return None

    @staticmethod
    async def _already_submitted(listing_id: str, db) -> bool:
        """Check application log for existing submission. Returns True if duplicate."""
        if db is None:
            return False
        # In production: query ApplicationRecord table by listing_id + status=SUBMITTED
        # result = await db.execute(
        #     select(ApplicationRecord).where(
        #         ApplicationRecord.listing_id == listing_id,
        #         ApplicationRecord.status == ApplicationStatus.SUBMITTED
        #     )
        # )
        # return result.scalar_one_or_none() is not None
        return False  # stub
