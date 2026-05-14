"""
Job Discovery Agent — the pipeline's entry point.

Flow:
  1. Build domain-specific search queries from master resume skills
  2. Select relevant platforms for each target country + domain
  3. Scrape all platforms in parallel (per-platform semaphore)
  4. Merge + deduplicate results
  5. Score each listing: keyword overlap × preference weight × deadline urgency
  6. Filter: min score threshold, deadline window, work-auth
  7. Return ranked list → JD Analyzer

Scoring formula per listing:
  fit_score = (keyword_overlap * 0.50)
            + (preference_weight * 0.30)
            + (deadline_urgency * 0.10)
            + (platform_prestige * 0.10)

Platform prestige tiers:
  Tier 1 (1.0): NSF REU, MITACS, DAAD, Euraxess, EMBL, OIST, A*STAR
  Tier 2 (0.7): Nature Careers, Science Careers, IEEE, ACM, FindAPhD
  Tier 3 (0.5): Handshake, Academics.de, Others
"""

from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from agents.base_agent import BaseAgent
from agents.job_discovery.platforms import PlatformConfig, get_platforms_for
from agents.job_discovery.scraper import UniversalScraper
from models.schemas import Country, JobListing, MasterResume, UserPrefs

log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────────────────────
MAX_CONCURRENT_SCRAPERS = 4   # parallel platform scrapers
MIN_FIT_SCORE = 0.25          # discard listings below this
MAX_RESULTS_PER_PLATFORM = 30

PLATFORM_PRESTIGE: dict[str, float] = {
    "nsf_reu":       1.0,
    "mitacs":        1.0,
    "daad":          1.0,
    "euraxess":      1.0,
    "embl_jobs":     1.0,
    "oist":          1.0,
    "astar":         1.0,
    "nature_careers": 0.7,
    "science_careers": 0.7,
    "ieee_jobs":     0.7,
    "acm_jobs":      0.7,
    "findaphd":      0.7,
    "handshake":     0.5,
    "academics_de":  0.5,
}


class JobDiscoveryAgent(BaseAgent):

    async def run(
        self,
        master: MasterResume,
        prefs: UserPrefs,
        proxy_url: str | None = None,
    ) -> list[JobListing]:
        """
        Returns ranked list of internship listings above MIN_FIT_SCORE.
        """
        queries = self._build_queries(master, prefs)
        log.info("discovery_start", queries=queries, countries=prefs.target_countries)

        # Select platforms for target countries + domains
        platforms = get_platforms_for(prefs.target_countries, prefs.target_domains)
        log.info("platforms_selected", count=len(platforms), ids=[p.id for p in platforms])

        # Scrape all platforms × queries in parallel (bounded)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPERS)
        tasks = []
        for platform in platforms:
            for country in prefs.target_countries:
                if country not in platform.countries and len(platform.countries) < 8:
                    continue
                for query in queries[:3]:  # top 3 queries per platform
                    tasks.append(
                        self._scrape_one(semaphore, platform, query, country, proxy_url)
                    )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge all listings
        all_listings: list[JobListing] = []
        for r in results:
            if isinstance(r, list):
                all_listings.extend(r)
            elif isinstance(r, Exception):
                log.warning("scrape_task_failed", error=str(r))

        log.info("raw_listings_merged", total=len(all_listings))

        # Score, filter, rank
        scored = [
            self._score(listing, master, prefs)
            for listing in all_listings
        ]
        filtered = [
            l for l in scored
            if l.fit_score >= MIN_FIT_SCORE
            and self._deadline_ok(l, prefs.min_deadline_days)
            and self._work_auth_ok(l, prefs)
        ]
        ranked = sorted(filtered, key=lambda l: l.fit_score, reverse=True)

        log.info("discovery_complete",
                 raw=len(all_listings), filtered=len(filtered), returned=len(ranked))
        return ranked

    # ── Scrape one platform ───────────────────────────────────────────────────

    async def _scrape_one(
        self,
        sem: asyncio.Semaphore,
        platform: PlatformConfig,
        query: str,
        country: Country,
        proxy_url: str | None,
    ) -> list[JobListing]:
        async with sem:
            try:
                scraper = UniversalScraper(platform, proxy_url)
                listings = await scraper.scrape(query, country, MAX_RESULTS_PER_PLATFORM)
                log.info("platform_scraped",
                         platform=platform.id, query=query, count=len(listings))
                return listings
            except Exception as e:
                log.warning("platform_scrape_error", platform=platform.id, error=str(e))
                return []

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score(self, listing: JobListing, master: MasterResume, prefs: UserPrefs) -> JobListing:
        """Compute fit_score in-place, return listing."""
        title_desc = (listing.title + " " + listing.description).lower()

        # 1. Keyword overlap (skills from master resume found in listing text)
        skill_hits = sum(1 for s in master.skills if s.lower() in title_desc)
        keyword_overlap = min(skill_hits / max(len(master.skills), 1), 1.0)

        # 2. Preference weight (country × domain from analytics feedback)
        country_w = prefs.country_weights.get(listing.country, 0.5)
        domain_w  = 0.5  # default; analytics agent updates per domain later
        for domain in prefs.target_domains:
            if domain.lower() in title_desc:
                domain_w = prefs.domain_weights.get(domain, 0.6)
                break
        pref_weight = (country_w + domain_w) / 2

        # 3. Deadline urgency (closer deadline = lower urgency = lower score penalty)
        deadline_urgency = 0.5
        if listing.deadline:
            days_left = (listing.deadline - datetime.now(timezone.utc)).days
            if days_left < 0:
                deadline_urgency = 0.0   # expired
            elif days_left < 14:
                deadline_urgency = 0.3   # too soon
            elif days_left < 60:
                deadline_urgency = 1.0   # sweet spot
            else:
                deadline_urgency = 0.7   # far away

        # 4. Platform prestige
        prestige = PLATFORM_PRESTIGE.get(listing.portal, 0.5)

        fit_score = (
            keyword_overlap * 0.50 +
            pref_weight    * 0.30 +
            deadline_urgency * 0.10 +
            prestige       * 0.10
        )

        listing.fit_score = round(fit_score, 4)
        return listing

    # ── Filters ───────────────────────────────────────────────────────────────

    @staticmethod
    def _deadline_ok(listing: JobListing, min_days: int) -> bool:
        if listing.deadline is None:
            return True   # no deadline = open rolling = fine
        return (listing.deadline - datetime.now(timezone.utc)).days >= min_days

    @staticmethod
    def _work_auth_ok(listing: JobListing, prefs: UserPrefs) -> bool:
        auth = prefs.work_auth.get(listing.country)
        if auth is None:
            return True   # unknown = don't filter out
        return True        # work auth validation happens in Router; here just pass

    # ── Query builder ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_queries(master: MasterResume, prefs: UserPrefs) -> list[str]:
        """
        Build 3–5 targeted search queries from resume skills + target domains.
        Short + specific → better search results than long phrases.
        """
        queries = set()

        # Domain-specific base queries
        for domain in prefs.target_domains[:3]:
            queries.add(f"{domain} research internship")

        # Skill-driven queries (top skills most likely to match JD titles)
        top_skills = master.skills[:6]
        if top_skills:
            queries.add(f"{' '.join(top_skills[:3])} internship")
            queries.add(f"research internship {top_skills[0]}")

        # Generic fallback
        queries.add("research internship STEM")

        return list(queries)[:5]
