"""
Universal scraper for research internship platforms.

Strategy: try plain HTTP first (fast, cheap), fall back to Playwright if the page
is JS-rendered and comes back empty. Most platforms are fine with httpx except
Handshake (needs browser) and a couple of EU portals.

Heads up on specific platforms:
  - NSF REU: table-based HTML from like 2005, selector is fragile
  - DAAD: works fine with httpx most of the time
  - Euraxess: JS-heavy, almost always needs Playwright fallback
  - Nature Careers: rate limits hard at ~10 req/min, be careful
  - OIST: only one search page, don't paginate

Anti-detection: rotating user agents + random sleep jitter.
We're not doing anything sketchy — just reading public job listings —
but their WAFs don't know that.

TODO: add cookie persistence so we don't get flagged as a bot on repeat runs
TODO: Handshake blocks headless Chromium sometimes, need proper stealth mode
FIXME: _parse_deadline is garbage for non-English date strings ("1. März 2025")
"""

import asyncio
import hashlib
import random
import re
import time
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup

from agents.job_discovery.platforms import PlatformConfig
from models.schemas import Country, JobListing

log = structlog.get_logger()

# rotating UA pool. not perfect but better than nothing.
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4.1 Safari/605.1.15",
]

# signs that we hit a CAPTCHA page instead of real content
CAPTCHA_HINTS = ["recaptcha", "hcaptcha", "cf-challenge", "access denied", "you've been blocked"]

# track seen listings across platforms to avoid dupes
# yeah it's a global, it's fine for a single process
_seen: set[str] = set()


def _dedupe_key(title: str, company: str, url: str) -> str:
    return hashlib.sha1(f"{title.lower()}{company.lower()}{url}".encode()).hexdigest()


class UniversalScraper:

    def __init__(self, platform: PlatformConfig, proxy: str | None = None):
        self.p = platform
        self.proxy = proxy
        self._domain = platform.base_url.split("/")[2]

    async def scrape(self, query: str, country: Country, limit: int = 50) -> list[JobListing]:
        results = []
        for page in range(1, self.p.max_pages + 1):
            if len(results) >= limit:
                break

            url = self.p.search_url_template.format(
                query=query.replace(" ", "+"),
                country=country.value,
                page=page,
            )

            # polite delay with jitter so we don't look like a hammer
            await asyncio.sleep(self.p.rate_limit_sec + random.uniform(0, 0.8))

            html = await self._get(url)
            if not html:
                break

            page_results = self._parse(html, country)
            if not page_results:
                break  # empty page = we've hit the end, stop paginating

            results.extend(page_results)
            log.info("scraped", platform=self.p.id, page=page, found=len(page_results))

        return results[:limit]

    async def _get(self, url: str) -> str | None:
        """Try httpx, fall back to Playwright if page looks empty/JS-rendered."""
        html = await self._httpx(url)
        if html and self._has_results(html):
            return html

        # httpx failed or got empty page — try browser
        log.info("playwright_fallback", platform=self.p.id, url=url[:60])
        try:
            from infra.metrics_hooks import record_listing_discovered  # lazy import
        except ImportError:
            pass
        return await self._playwright(url)

    async def _httpx(self, url: str) -> str | None:
        headers = {
            "User-Agent": random.choice(UAS),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
        }
        try:
            proxy = self.proxy if self.proxy else None
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=15.0,
                headers=headers, proxy=proxy,
            ) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.text
                if r.status_code == 429:
                    # rate limited — back off hard
                    log.warning("rate_limited", platform=self.p.id, sleeping=60)
                    await asyncio.sleep(60)
                return None
        except Exception as e:
            log.debug("httpx_fail", url=url[:60], err=str(e))
            return None

    async def _playwright(self, url: str) -> str | None:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
                ctx = await browser.new_context(
                    user_agent=random.choice(UAS),
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                )
                page = await ctx.new_page()
                # hide the webdriver flag — some sites check for it
                await page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                # small human-like pause
                await asyncio.sleep(random.uniform(1.5, 3.0))
                html = await page.content()
                await browser.close()
                return html
        except Exception as e:
            log.warning("playwright_fail", url=url[:60], err=str(e))
            return None

    def _parse(self, html: str, country: Country) -> list[JobListing]:
        # bail if we're looking at a CAPTCHA page
        if any(s in html.lower() for s in CAPTCHA_HINTS):
            log.warning("captcha_page", platform=self.p.id)
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(self.p.result_selector)
        out = []

        for card in cards:
            try:
                title   = _text(card, self.p.title_selector)
                company = _text(card, self.p.company_selector)
                link    = _href(card, self.p.link_selector, self.p.base_url)
                desc    = _text(card, self.p.description_selector)
                deadline_str = _text(card, self.p.deadline_selector) if self.p.deadline_selector else None

                if not title or not link:
                    continue

                key = _dedupe_key(title, company, link)
                if key in _seen:
                    continue
                _seen.add(key)

                out.append(JobListing(
                    id=key[:16],
                    title=title,
                    company=company or self.p.name,
                    country=country,
                    portal=self.p.id,
                    url=link,
                    description=desc[:3000],
                    deadline=_parse_date(deadline_str),
                ))
            except Exception as e:
                log.debug("card_parse_err", platform=self.p.id, err=str(e))

        return out

    def _has_results(self, html: str) -> bool:
        return len(BeautifulSoup(html, "lxml").select(self.p.result_selector)) > 0


# ── tiny helpers so the parse loop stays readable ─────────────────────────────

def _text(tag, sel: str) -> str:
    if not sel:
        return ""
    el = tag.select_one(sel)
    return el.get_text(strip=True) if el else ""

def _href(tag, sel: str, base: str) -> str:
    el = tag.select_one(sel)
    if not el:
        return ""
    href = el.get("href", "")
    if href.startswith("http"):
        return href
    return f"{base.rstrip('/')}/{href.lstrip('/')}"

def _parse_date(text: str | None) -> datetime | None:
    """
    Best-effort date parser. Works for most EN/EU formats.
    Will silently return None for anything weird — caller handles it.
    TODO: handle German "1. März 2025" format
    """
    if not text:
        return None
    for pat in [
        r"(\d{4})-(\d{2})-(\d{2})",     # ISO
        r"(\d{2})[./](\d{2})[./](\d{4})", # EU style
    ]:
        m = re.search(pat, text)
        if m:
            try:
                g = m.groups()
                if len(g[0]) == 4:
                    return datetime(int(g[0]), int(g[1]), int(g[2]))
                return datetime(int(g[2]), int(g[1]), int(g[0]))
            except ValueError:
                pass
    return None
