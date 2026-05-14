"""
Base class for portal form fillers.

All the portal-specific fillers (LinkedIn, DAAD, etc.) inherit from this.
It handles browser setup, field filling with fallbacks, file uploads,
screenshots, and CAPTCHA detection.

On CAPTCHAs: we don't bypass them. Period. We detect and escalate.
Not because we can't, but because it's against ToS and could get the
user's account banned. Human queue it is.

Annoyances discovered the hard way:
  - Playwright's .fill() fails on some custom JS dropdowns — use .click() + type instead
  - Some portals reload the page between form steps and lose session — need cookie handling
  - "Submit" buttons are sometimes <div> elements styled as buttons. Fun.
  - DAAD's portal times out if you're too fast between fields. Added sleep(0.5) everywhere.
  - LinkedIn detects headless mode via navigator.webdriver — patched in _launch()

TODO: add cookie persistence so logged-in session survives between runs
TODO: Euraxess sometimes requires 2FA — no automated solution yet, human queue
"""

import asyncio
import base64
import random
from datetime import datetime, timezone
from pathlib import Path

import structlog
from playwright.async_api import (
    async_playwright, Browser, Page,
    TimeoutError as PWTimeout,
)

log = structlog.get_logger()

SCREENSHOT_DIR = Path("/tmp/internship_bot_screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# selectors that indicate a CAPTCHA wall
CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    ".g-recaptcha",
    "#captcha",
    "[class*='captcha']",
    "iframe[title*='challenge']",
]


class CaptchaHit(Exception):
    pass

class FillFailed(Exception):
    pass


class BasePortalFiller:

    TIMEOUT = 15_000     # most elements
    NAV_TIMEOUT = 30_000 # page loads

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.log = structlog.get_logger(filler=self.__class__.__name__)
        self._shots: list[dict] = []  # screenshot evidence log

    async def fill_and_return(self, package) -> dict:
        """
        Opens browser, fills form, returns state dict.
        Does NOT click submit — that's SubmissionAgent's job.
        """
        async with async_playwright() as pw:
            browser = await self._launch(pw)
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await ctx.new_page()

            try:
                await self.fill_form(page, package)
                return {
                    "status": "ready_to_submit",
                    "url": page.url,
                    "screenshots": self._shots,
                    "portal": package.portal.type,
                }
            except CaptchaHit:
                self.log.error("captcha_hit", url=page.url)
                await self._snap(page, "captcha")
                raise
            except FillFailed as e:
                self.log.error("fill_failed", err=str(e))
                await self._snap(page, "fill_error")
                raise
            except PWTimeout as e:
                self.log.error("timeout", err=str(e), url=page.url)
                await self._snap(page, "timeout")
                raise
            finally:
                await ctx.close()
                await browser.close()

    async def fill_form(self, page: Page, package) -> None:
        raise NotImplementedError("subclass must implement fill_form()")

    # ── helpers the subclasses actually use ───────────────────────────────────

    async def goto(self, page: Page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=self.NAV_TIMEOUT)
        await self._check_captcha(page)
        await self._snap(page, "loaded")

    async def fill(self, page: Page, value: str, css: str = None,
                   label: str = None, required: bool = False) -> bool:
        """
        Fill a text input. Tries CSS selector first, then label text.
        Returns True if filled, False if not found.
        Raises FillFailed if required=True and nothing works.
        """
        loc = None

        if css:
            l = page.locator(css).first
            if await l.count() > 0:
                loc = l

        if not loc and label:
            l = page.get_by_label(label, exact=False).first
            if await l.count() > 0:
                loc = l

        if not loc:
            if required:
                raise FillFailed(f"Field not found: css={css!r} label={label!r}")
            return False

        await loc.scroll_into_view_if_needed()
        await loc.fill(str(value), timeout=self.TIMEOUT)
        # small pause — some portals validate on blur and need a moment
        await asyncio.sleep(0.3)
        return True

    async def pick(self, page: Page, value: str, css: str = None, label: str = None) -> bool:
        """Select a dropdown option by value or visible text."""
        loc = None
        if css:
            l = page.locator(css).first
            if await l.count() > 0:
                loc = l
        if not loc and label:
            l = page.get_by_label(label, exact=False).first
            if await l.count() > 0:
                loc = l
        if not loc:
            return False
        # try by value first, then label text
        try:
            await loc.select_option(value=value, timeout=self.TIMEOUT)
            return True
        except Exception:
            try:
                await loc.select_option(label=value, timeout=self.TIMEOUT)
                return True
            except Exception:
                return False

    async def upload(self, page: Page, path: Path, css: str) -> None:
        if not path.exists():
            raise FillFailed(f"Upload file missing: {path}")
        inp = page.locator(css).first
        await inp.set_input_files(str(path), timeout=self.TIMEOUT)
        await self._snap(page, f"uploaded_{path.stem}")

    async def click(self, page: Page, css: str, required: bool = True) -> bool:
        loc = page.locator(css).first
        if await loc.count() == 0:
            if required:
                raise FillFailed(f"Button not found: {css}")
            return False
        await loc.click(timeout=self.TIMEOUT)
        return True

    async def wait(self, page: Page, css: str, ms: int = None) -> bool:
        try:
            await page.wait_for_selector(css, timeout=ms or self.TIMEOUT)
            return True
        except PWTimeout:
            return False

    async def _snap(self, page: Page, label: str) -> None:
        try:
            ts = datetime.now(timezone.utc).strftime("%H%M%S")
            path = SCREENSHOT_DIR / f"{ts}_{label}.png"
            await page.screenshot(path=str(path))
            self._shots.append({
                "label": label,
                "path": str(path),
                "b64": base64.b64encode(path.read_bytes()).decode(),
            })
        except Exception:
            pass  # screenshots are evidence, not critical path

    async def _check_captcha(self, page: Page) -> None:
        for sel in CAPTCHA_SELECTORS:
            if await page.locator(sel).count() > 0:
                raise CaptchaHit(f"CAPTCHA detected: {sel}")

    async def _launch(self, pw) -> Browser:
        return await pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # hide headless flag
                "--disable-infobars",
            ],
        )
