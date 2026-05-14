"""
JD Analyzer Agent — extracts ATS keyword weights + country format rules.
Calls LLM once per listing. Output feeds both Resume Customizer and ATS Scorer.
"""

from __future__ import annotations
import json
import re

from agents.base_agent import BaseAgent, with_retry
from models.schemas import (
    Country,
    CountryFormatRules,
    JDAnalysis,
    JobListing,
    KeywordWeight,
)

SYSTEM_PROMPT = """
You are an expert ATS (Applicant Tracking System) analyst.
Extract structured information from the job description provided.
Output ONLY valid JSON — no markdown, no preamble.

Output schema:
{
  "keywords": [
    {"keyword": "python", "weight": 0.9, "required": true, "section_origin": "requirements"},
    ...
  ],
  "required_skills": ["python", "pytorch"],
  "preferred_skills": ["docker", "kubernetes"],
  "tone_signals": ["formal", "research-focused"],
  "company_culture_notes": "..."
}

Rules:
- weight: 0–1 (1.0 = in job title, 0.9 = in requirements, 0.6 = in preferred, 0.4 = in description body)
- required: true only if explicitly stated as required/must-have
- keywords: include tools, techniques, programming languages, domain terms, soft skills if explicitly listed
- max 30 keywords
- Use lowercase for all keywords
"""

# Country → format rules (hardcoded truth)
COUNTRY_FORMAT_MAP: dict[Country, CountryFormatRules] = {
    Country.USA: CountryFormatRules(
        include_photo=False, include_address=False, max_pages=1,
        date_format="MM/YYYY", europass_format=False,
    ),
    Country.CANADA: CountryFormatRules(
        include_photo=False, include_address=False, max_pages=2,
        date_format="MM/YYYY",
    ),
    Country.GERMANY: CountryFormatRules(
        include_photo=True, include_address=True, max_pages=2,
        date_format="MM.YYYY", cover_letter_tone="formal",
    ),
    Country.NETHERLANDS: CountryFormatRules(
        include_photo=False, include_address=False, max_pages=2,
        cover_letter_tone="direct",
    ),
    Country.FRANCE: CountryFormatRules(
        include_photo=True, include_address=True, max_pages=2,
        date_format="MM/YYYY",
    ),
    Country.SWEDEN: CountryFormatRules(
        include_photo=False, include_address=False, max_pages=2,
        cover_letter_tone="direct",
    ),
}


class JDAnalyzerAgent(BaseAgent):

    @with_retry(max_attempts=3)
    async def run(self, listing: JobListing) -> JDAnalysis:
        raw = await self._llm(
            system=SYSTEM_PROMPT,
            user=f"Job title: {listing.title}\nCompany: {listing.company}\nCountry: {listing.country}\n\n{listing.description}",
            max_tokens=1500,
        )
        return self._parse(raw, listing)

    def _parse(self, raw: str, listing: JobListing) -> JDAnalysis:
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            self.log.warning("jd_parse_failure", listing_id=listing.id)
            data = {"keywords": [], "required_skills": [], "preferred_skills": []}

        keywords = [
            KeywordWeight(**kw)
            for kw in data.get("keywords", [])
        ]

        fmt_rules = COUNTRY_FORMAT_MAP.get(
            listing.country,
            CountryFormatRules()  # safe defaults
        )

        return JDAnalysis(
            listing=listing,
            keywords=keywords,
            required_skills=data.get("required_skills", []),
            preferred_skills=data.get("preferred_skills", []),
            format_rules=fmt_rules,
            tone_signals=data.get("tone_signals", []),
            company_summary=data.get("company_culture_notes"),
        )
