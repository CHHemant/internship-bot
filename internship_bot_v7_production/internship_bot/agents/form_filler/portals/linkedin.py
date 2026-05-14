"""
LinkedIn Easy Apply Filler.

Flow:
  1. Navigate to job URL
  2. Check for "Easy Apply" button → if absent, route to full apply (unimplemented, flag)
  3. Click Easy Apply → modal opens
  4. Step through modal pages:
     - Contact info (pre-filled from profile, verify)
     - Resume upload (our custom resume)
     - Additional questions (work auth, availability, GPA)
     - Review page → stop here (Submission Agent clicks final submit)

NOTE ON SELECTORS:
  LinkedIn DOM changes frequently. Selectors here are accurate as of mid-2025.
  If fill_form() fails on a selector, the error is caught, logged, and escalated.
  Do NOT hardcode brittle nth-child chains — use aria-labels + role selectors.
"""

from __future__ import annotations
import asyncio
from pathlib import Path

from playwright.async_api import Page

from agents.form_filler.base_filler import BasePortalFiller, FormFillError


# LinkedIn login URL (use stored credentials from env)
LINKEDIN_LOGIN = "https://www.linkedin.com/login"

# Easy Apply modal selectors (aria-label based — more stable than CSS class chains)
SEL_EASY_APPLY_BTN = "button:has-text('Easy Apply')"
SEL_NEXT_BTN       = "button:has-text('Next')"
SEL_REVIEW_BTN     = "button:has-text('Review')"
SEL_RESUME_UPLOAD  = "input[type='file']"
SEL_PHONE_FIELD    = "input[id*='phoneNumber']"
SEL_WORK_AUTH      = "select[id*='workAuthorization'], select[id*='authorization']"
SEL_YEARS_EXP      = "input[id*='yearsOfExperience'], select[id*='yearsOfExperience']"
SEL_MODAL          = "div[data-test-modal]"
SEL_PROGRESS_BAR   = "[aria-label*='progress']"


class LinkedInFiller(BasePortalFiller):

    async def fill_form(self, page: Page, package) -> None:
        from config.settings import settings

        # ── 1. Login ──────────────────────────────────────────────────────────
        await self.navigate(page, LINKEDIN_LOGIN)
        await self.fill_field(page, settings.LINKEDIN_EMAIL,
                              css="input#username", required=True)
        await self.fill_field(page, settings.LINKEDIN_PASSWORD,
                              css="input#password", required=True)
        await self.click(page, "button[data-litms-control-urn*='login']")
        await page.wait_for_url("**/feed**", timeout=20_000)
        await self.screenshot(page, "linkedin_logged_in")

        # ── 2. Navigate to job ────────────────────────────────────────────────
        await self.navigate(page, str(package.listing.url))
        await self.screenshot(page, "job_page")

        # ── 3. Easy Apply button ──────────────────────────────────────────────
        easy_apply_present = await self.wait_for_selector(page, SEL_EASY_APPLY_BTN, timeout_ms=6000)
        if not easy_apply_present:
            raise FormFillError(
                "Easy Apply button not found — listing may require full application. "
                "Flag for human or implement full-apply flow."
            )
        await self.click(page, SEL_EASY_APPLY_BTN)
        await self.wait_for_selector(page, SEL_MODAL, timeout_ms=8000)
        await self.screenshot(page, "easy_apply_modal_open")

        # ── 4. Step through modal pages ───────────────────────────────────────
        field_map = package.field_map
        step = 0
        max_steps = 10  # safety cap

        while step < max_steps:
            step += 1
            await asyncio.sleep(0.8)  # let page settle

            # Phone number (step 1 usually)
            await self.fill_field(page, field_map.get("phone", ""),
                                  css=SEL_PHONE_FIELD)

            # Resume upload — always upload our custom version
            resume_input = page.locator(SEL_RESUME_UPLOAD).first
            if await resume_input.count() > 0:
                await resume_input.set_input_files(str(package.resume_path))
                await self.screenshot(page, f"resume_uploaded_step{step}")

            # Work authorization
            auth_value = "1" if field_map.get("work_authorization") == "yes" else "0"
            await self.select_option(page, auth_value, css=SEL_WORK_AUTH)

            # Years of experience
            await self.fill_field(page, field_map.get("years_experience", "1"),
                                  css=SEL_YEARS_EXP)

            # Cover letter text area (some postings have one)
            await self.fill_field(
                page,
                package.cover_letter_path.read_text(encoding="utf-8")[:2000],
                label_text="cover letter",
            )

            await self.screenshot(page, f"modal_step_{step}_filled")

            # Try Next → then Review → stop before Submit
            if await self.wait_for_selector(page, SEL_REVIEW_BTN, timeout_ms=2000):
                await self.click(page, SEL_REVIEW_BTN)
                await self.screenshot(page, "review_page")
                break

            if await self.wait_for_selector(page, SEL_NEXT_BTN, timeout_ms=2000):
                await self.click(page, SEL_NEXT_BTN)
            else:
                # No Next or Review — we're either done or stuck
                self.log.warning("linkedin_no_next_button", step=step)
                break

        await self.screenshot(page, "linkedin_preflight_complete")
        self.log.info("linkedin_ready_for_submission", steps=step)
