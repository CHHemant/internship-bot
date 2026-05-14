"""
Portal fillers for: DAAD · Euraxess · Handshake · University (generic)

Each inherits BasePortalFiller and implements fill_form().
University filler is the most defensive — every selector has a fallback.
"""

from __future__ import annotations
import asyncio
from pathlib import Path

from playwright.async_api import Page

from agents.form_filler.base_filler import BasePortalFiller, FormFillError


# ═════════════════════════════════════════════════════════════════════════════
# DAAD Filler (daad.de Bewerbungsportal)
# ═════════════════════════════════════════════════════════════════════════════

DAAD_PORTAL   = "https://www.daad.de/en/study-and-research-in-germany/scholarships/"
DAAD_LOGIN    = "https://oasis.daad.de"


class DAADFiller(BasePortalFiller):

    async def fill_form(self, page: Page, package) -> None:
        from config.settings import settings
        field_map = package.field_map

        # ── Login ─────────────────────────────────────────────────────────────
        await self.navigate(page, DAAD_LOGIN)
        await self.fill_field(page, settings.DAAD_EMAIL,
                              css="input[name='username'], input[type='email']",
                              required=True)
        await self.fill_field(page, settings.DAAD_PASSWORD,
                              css="input[name='password'], input[type='password']",
                              required=True)
        await self.click(page, "button[type='submit'], input[type='submit']")
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await self.screenshot(page, "daad_logged_in")

        # ── Navigate to application form ──────────────────────────────────────
        await self.navigate(page, str(package.listing.url))
        await self.screenshot(page, "daad_application_page")

        # ── Personal data section ─────────────────────────────────────────────
        # DAAD uses DD.MM.YYYY date fields — convert graduation year
        grad_year = field_map.get("graduation_year", "")

        await self.fill_field(page, field_map["first_name"],
                              label_text="Vorname", css="input[name*='firstname'], input[name*='firstName']")
        await self.fill_field(page, field_map["last_name"],
                              label_text="Nachname", css="input[name*='lastname'], input[name*='lastName']")
        await self.fill_field(page, field_map["email"],
                              label_text="E-Mail", css="input[type='email']")
        await self.fill_field(page, field_map.get("phone", ""),
                              label_text="Telefon", css="input[name*='phone']")
        await self.screenshot(page, "daad_personal_data")

        # ── Academic background ───────────────────────────────────────────────
        await self.fill_field(page, field_map.get("institution", ""),
                              label_text="Hochschule", css="input[name*='university']")
        await self.fill_field(page, field_map.get("highest_degree", ""),
                              label_text="Abschluss", css="select[name*='degree']")
        await self.screenshot(page, "daad_academic")

        # ── Document uploads ──────────────────────────────────────────────────
        # CV upload
        cv_inputs = await page.locator("input[type='file']").all()
        if cv_inputs:
            await cv_inputs[0].set_input_files(str(package.resume_path))
            await self.screenshot(page, "daad_cv_uploaded")

        # Funding statement (DAAD-specific, mandatory)
        funding_path = package.extra_docs.get("funding_statement")
        if funding_path and len(cv_inputs) > 1:
            await cv_inputs[1].set_input_files(str(funding_path))
            await self.screenshot(page, "daad_funding_uploaded")
        elif not funding_path:
            self.log.warning("daad_funding_statement_missing")

        # ── Motivation / cover letter text area ───────────────────────────────
        cover_text = package.cover_letter_path.read_text(encoding="utf-8")
        await self.fill_field(page, cover_text[:3000],
                              css="textarea[name*='motivation'], textarea[id*='motivation']",
                              label_text="Motivationsschreiben")

        await self.screenshot(page, "daad_preflight_complete")
        self.log.info("daad_ready_for_submission")


# ═════════════════════════════════════════════════════════════════════════════
# Euraxess Filler (euraxess.ec.europa.eu)
# ═════════════════════════════════════════════════════════════════════════════

class EuraxessFiller(BasePortalFiller):

    async def fill_form(self, page: Page, package) -> None:
        from config.settings import settings
        field_map = package.field_map

        # ── Login ─────────────────────────────────────────────────────────────
        await self.navigate(page, "https://euraxess.ec.europa.eu/user/login")
        await self.fill_field(page, settings.EURAXESS_EMAIL,
                              css="input#edit-name", required=True)
        await self.fill_field(page, settings.EURAXESS_PASSWORD,
                              css="input#edit-pass", required=True)
        await self.click(page, "input#edit-submit")
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await self.screenshot(page, "euraxess_logged_in")

        # ── Go to job posting ─────────────────────────────────────────────────
        await self.navigate(page, str(package.listing.url))
        await self.click(page, "a:has-text('Apply'), button:has-text('Apply')", required=True)
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        await self.screenshot(page, "euraxess_apply_form")

        # ── Personal info ─────────────────────────────────────────────────────
        await self.fill_field(page, field_map["first_name"],
                              css="input[name*='first'], input[id*='first']")
        await self.fill_field(page, field_map["last_name"],
                              css="input[name*='last'], input[id*='last']")
        await self.fill_field(page, field_map["email"],
                              css="input[type='email']")
        await self.screenshot(page, "euraxess_personal")

        # ── Research field taxonomy dropdown ──────────────────────────────────
        # Euraxess uses a specific taxonomy code — we pick the closest match
        # Best effort: select "Computer Science" or "Biology" depending on JD
        research_field_css = "select[name*='research_field'], select[id*='field']"
        await self.select_option(page, "Computer Science", css=research_field_css)

        # Career stage
        await self.select_option(page, "R1",  # R1 = first stage researcher (student)
                                 css="select[name*='career_stage'], select[id*='career']")

        # ── Europass CV upload ────────────────────────────────────────────────
        europass_path = package.extra_docs.get("europass_cv", package.resume_path)
        file_inputs = await page.locator("input[type='file']").all()
        if file_inputs:
            await file_inputs[0].set_input_files(str(europass_path))
            await self.screenshot(page, "euraxess_cv_uploaded")

        # ── Motivation letter upload ──────────────────────────────────────────
        if len(file_inputs) > 1:
            await file_inputs[1].set_input_files(str(package.cover_letter_path))
            await self.screenshot(page, "euraxess_cover_uploaded")

        await self.screenshot(page, "euraxess_preflight_complete")
        self.log.info("euraxess_ready_for_submission")


# ═════════════════════════════════════════════════════════════════════════════
# Handshake Filler (joinhandshake.com — USA/Canada)
# ═════════════════════════════════════════════════════════════════════════════

class HandshakeFiller(BasePortalFiller):

    async def fill_form(self, page: Page, package) -> None:
        from config.settings import settings
        field_map = package.field_map

        # ── Login ─────────────────────────────────────────────────────────────
        await self.navigate(page, "https://app.joinhandshake.com/login")
        await self.fill_field(page, settings.HANDSHAKE_EMAIL,
                              css="input[type='email']", required=True)
        await self.click(page, "button:has-text('Next'), button[type='submit']")
        await asyncio.sleep(1)
        await self.fill_field(page, settings.HANDSHAKE_PASSWORD,
                              css="input[type='password']", required=True)
        await self.click(page, "button[type='submit']")
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await self.screenshot(page, "handshake_logged_in")

        # ── Job page ──────────────────────────────────────────────────────────
        await self.navigate(page, str(package.listing.url))
        await self.screenshot(page, "handshake_job_page")

        # Try Easy Apply first
        easy_apply = await self.wait_for_selector(
            page, "button:has-text('Apply'), a:has-text('Apply')", timeout_ms=5000
        )
        if easy_apply:
            await self.click(page, "button:has-text('Apply')")
            await asyncio.sleep(1)

        # ── Fill standard fields ──────────────────────────────────────────────
        await self.fill_field(page, field_map["first_name"],
                              label_text="First Name", css="input[name*='first_name']")
        await self.fill_field(page, field_map["last_name"],
                              label_text="Last Name", css="input[name*='last_name']")
        await self.fill_field(page, field_map["email"],
                              css="input[type='email']")
        await self.fill_field(page, field_map.get("phone", ""),
                              label_text="Phone", css="input[type='tel']")

        # GPA field (Handshake-specific)
        await self.fill_field(page, field_map.get("gpa", ""),
                              label_text="GPA", css="input[name*='gpa']")

        # Graduation date
        await self.fill_field(page, field_map.get("graduation_year", ""),
                              label_text="Graduation", css="input[name*='graduation']")

        # Work authorization dropdown
        auth_text = "F-1 OPT" if field_map.get("work_authorization") == "no" else "US Citizen/Permanent Resident"
        await self.select_option(page, auth_text,
                                 label_text="Work Authorization", css="select[name*='authorization']")

        # Resume upload
        file_inputs = await page.locator("input[type='file']").all()
        if file_inputs:
            await file_inputs[0].set_input_files(str(package.resume_path))
            await self.screenshot(page, "handshake_resume_uploaded")

        await self.screenshot(page, "handshake_preflight_complete")
        self.log.info("handshake_ready_for_submission")


# ═════════════════════════════════════════════════════════════════════════════
# University Generic Filler (*.edu, *.ac.uk, *.uni-xxx.de, etc.)
# ═════════════════════════════════════════════════════════════════════════════

# Ordered fallback selectors — try each until one works
GENERIC_NAME_SELECTORS = [
    "input[name*='name']", "input[id*='name']",
    "input[placeholder*='name' i]", "input[aria-label*='name' i]",
]
GENERIC_EMAIL_SELECTORS = [
    "input[type='email']", "input[name*='email']",
    "input[id*='email']", "input[placeholder*='email' i]",
]
GENERIC_FILE_SELECTOR = "input[type='file']"
GENERIC_TEXTAREA_SELECTORS = [
    "textarea[name*='cover']", "textarea[id*='cover']",
    "textarea[name*='letter']", "textarea[aria-label*='cover' i]",
    "textarea",  # last resort: first textarea on page
]


class UniversityFiller(BasePortalFiller):
    """
    Generic filler for university career portals.
    Uses ordered fallback selectors — no portal is the same.
    Always flags for human review before submission (set in Router).
    """

    async def fill_form(self, page: Page, package) -> None:
        from config.settings import settings
        field_map = package.field_map

        # ── Navigate to application ───────────────────────────────────────────
        await self.navigate(page, str(package.listing.url))
        await self.screenshot(page, "university_landing")

        # Try to find an "Apply" link
        for btn_text in ["Apply Now", "Apply", "Start Application", "Submit Application"]:
            btn = page.locator(f"a:has-text('{btn_text}'), button:has-text('{btn_text}')").first
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(1.5)
                break

        await self.screenshot(page, "university_apply_form")

        # ── Name field (try multiple selectors) ──────────────────────────────
        for sel in GENERIC_NAME_SELECTORS:
            if await self.fill_field(page, field_map["full_name"], css=sel):
                break

        # ── Email field ───────────────────────────────────────────────────────
        for sel in GENERIC_EMAIL_SELECTORS:
            if await self.fill_field(page, field_map["email"], css=sel):
                break

        # ── Phone (optional) ──────────────────────────────────────────────────
        await self.fill_field(page, field_map.get("phone", ""),
                              css="input[type='tel'], input[name*='phone']")

        # ── File uploads ──────────────────────────────────────────────────────
        file_inputs = await page.locator(GENERIC_FILE_SELECTOR).all()
        uploaded = 0
        for inp in file_inputs[:3]:  # upload max 3 files
            try:
                path = package.resume_path if uploaded == 0 else package.cover_letter_path
                await inp.set_input_files(str(path))
                uploaded += 1
                await self.screenshot(page, f"university_upload_{uploaded}")
            except Exception as e:
                self.log.warning("file_upload_failed", error=str(e))

        # ── Cover letter text area ────────────────────────────────────────────
        cover_text = package.cover_letter_path.read_text(encoding="utf-8")
        for sel in GENERIC_TEXTAREA_SELECTORS:
            ta = page.locator(sel).first
            if await ta.count() > 0:
                await ta.fill(cover_text[:3000])
                await self.screenshot(page, "university_cover_filled")
                break

        # ── Log session notes ─────────────────────────────────────────────────
        for note in package.session_notes:
            self.log.info("session_note", note=note)

        await self.screenshot(page, "university_preflight_complete")
        self.log.info("university_ready_for_human_review")
