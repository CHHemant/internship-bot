"""
Analytics Agent — the brain that makes the pipeline smarter over time.

Runs after every 10 submissions (configurable).
Analyses: response rate by (country × domain × company_size).
Outputs:  updated preference weights → Job Discovery + JD Analyzer.

Metrics tracked:
  - response_rate[country][domain]         → adjust country/domain weights
  - ats_score vs interview rate            → tune keyword targeting
  - cover_letter_tone vs response rate     → tune tone per country
  - company_size vs response rate          → filter targeting

Feedback loop:
  Analytics → UserPrefs.country_weights / domain_weights
  → Job Discovery Agent re-ranks listings next cycle
  → JD Analyzer adjusts keyword weights next cycle
"""

from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean

import structlog

from agents.base_agent import BaseAgent
from models.schemas import ApplicationRecord, ApplicationStatus, Country, UserPrefs

log = structlog.get_logger()

MIN_SAMPLES = 5          # skip analytics if fewer apps submitted
ANALYTICS_EVERY_N = 10  # run cycle every N submissions


class AnalyticsAgent(BaseAgent):

    async def run(
        self,
        records: list[ApplicationRecord],
        prefs: UserPrefs,
    ) -> UserPrefs:
        """
        Analyse all records, update preference weights, return updated prefs.
        Returns prefs unchanged if not enough data.
        """
        submitted = [r for r in records if r.status != ApplicationStatus.QUEUED]

        if len(submitted) < MIN_SAMPLES:
            self.log.info("analytics_skipped_insufficient_data", count=len(submitted))
            return prefs

        self.log.info("analytics_cycle_start", total_apps=len(submitted))

        metrics = self._compute_metrics(submitted)
        updated_prefs = self._update_weights(prefs, metrics)

        await self._emit_report(metrics, submitted)

        self.log.info(
            "analytics_cycle_complete",
            country_weights=updated_prefs.country_weights,
            domain_weights=updated_prefs.domain_weights,
        )
        return updated_prefs

    # ─── Metric computation ───────────────────────────────────────────────────

    def _compute_metrics(self, records: list[ApplicationRecord]) -> dict:
        """
        Returns nested dict with response rates and ATS correlations.
        """
        # Response = any status better than SUBMITTED
        positive_statuses = {
            ApplicationStatus.VIEWED,
            ApplicationStatus.INTERVIEW,
            ApplicationStatus.OFFER,
        }

        # country → list of (responded: bool, ats_score: float)
        country_data: dict[str, list[dict]] = defaultdict(list)
        domain_data:  dict[str, list[bool]] = defaultdict(list)
        ats_scores: list[tuple[float, bool]] = []  # (score, responded)

        for r in records:
            responded = r.status in positive_statuses
            country = r.listing.country.value
            domain  = r.listing.title.split()[0].lower()  # crude domain proxy
            ats_score = r.verification.ats.score if r.verification else 0.0

            country_data[country].append({"responded": responded, "ats": ats_score})
            domain_data[domain].append(responded)
            ats_scores.append((ats_score, responded))

        # Response rates per country
        country_response_rate = {
            country: mean(d["responded"] for d in data)
            for country, data in country_data.items()
            if data
        }

        # Response rates per domain
        domain_response_rate = {
            domain: mean(responded_list)
            for domain, responded_list in domain_data.items()
            if responded_list
        }

        # ATS score correlation: do higher-scoring apps get more responses?
        high_ats = [r for s, r in ats_scores if s >= 80]
        low_ats  = [r for s, r in ats_scores if s < 70]
        ats_correlation = {
            "high_ats_response_rate": mean(high_ats) if high_ats else 0,
            "low_ats_response_rate":  mean(low_ats)  if low_ats  else 0,
            "sample_high": len(high_ats),
            "sample_low":  len(low_ats),
        }

        return {
            "country_response_rate": country_response_rate,
            "domain_response_rate":  domain_response_rate,
            "ats_correlation":       ats_correlation,
            "total_apps":            len(records),
            "total_responded":       sum(1 for r in records if r.status in positive_statuses),
            "computed_at":           datetime.now(timezone.utc).isoformat(),
        }

    # ─── Weight update ────────────────────────────────────────────────────────

    def _update_weights(self, prefs: UserPrefs, metrics: dict) -> UserPrefs:
        """
        Normalise response rates → weights (0–1).
        Higher response rate country/domain gets higher weight in Discovery.
        """
        country_rates = metrics["country_response_rate"]
        domain_rates  = metrics["domain_response_rate"]

        def normalise(rate_dict: dict[str, float]) -> dict[str, float]:
            if not rate_dict:
                return {}
            max_rate = max(rate_dict.values()) or 1.0
            return {k: round(v / max_rate, 3) for k, v in rate_dict.items()}

        new_country_weights = normalise(country_rates)
        new_domain_weights  = normalise(domain_rates)

        # Blend with existing weights (70% new, 30% old) — avoid wild swings
        blended_country = {}
        for country in set(list(new_country_weights) + [c.value for c in prefs.target_countries]):
            old = prefs.country_weights.get(Country(country), 0.5) if country in [c.value for c in Country] else 0.5
            new = new_country_weights.get(country, 0.5)
            blended_country[Country(country)] = round(0.7 * new + 0.3 * old, 3)

        blended_domain = {}
        for domain in set(list(new_domain_weights) + list(prefs.domain_weights)):
            old = prefs.domain_weights.get(domain, 0.5)
            new = new_domain_weights.get(domain, 0.5)
            blended_domain[domain] = round(0.7 * new + 0.3 * old, 3)

        # Return updated copy
        updated = prefs.model_copy(
            update={
                "country_weights": blended_country,
                "domain_weights":  blended_domain,
            }
        )

        self.log.info(
            "weights_updated",
            country_weights=blended_country,
            domain_weights=blended_domain,
        )
        return updated

    # ─── Report emission ──────────────────────────────────────────────────────

    async def _emit_report(self, metrics: dict, records: list[ApplicationRecord]) -> None:
        """
        Log full analytics report. In production: email user + write to DB.
        """
        total = metrics["total_apps"]
        responded = metrics["total_responded"]
        rate = responded / total if total else 0

        report_lines = [
            f"=== Analytics Report ({datetime.now(timezone.utc).strftime('%Y-%m-%d')}) ===",
            f"Total applications: {total}",
            f"Responses received: {responded} ({rate:.0%})",
            "",
            "Response rate by country:",
        ]
        for country, cr in sorted(
            metrics["country_response_rate"].items(), key=lambda x: -x[1]
        ):
            report_lines.append(f"  {country.upper():12s} {cr:.0%}")

        report_lines += [
            "",
            "ATS score correlation:",
            f"  High ATS (≥80): {metrics['ats_correlation']['high_ats_response_rate']:.0%} response rate"
            f"  (n={metrics['ats_correlation']['sample_high']})",
            f"  Low ATS (<70):  {metrics['ats_correlation']['low_ats_response_rate']:.0%} response rate"
            f"  (n={metrics['ats_correlation']['sample_low']})",
        ]

        report = "\n".join(report_lines)
        self.log.info("analytics_report", report=report)
        # TODO: send report via email to user, write to analytics table
