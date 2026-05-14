"""
Cover Letter Agent — generates culture-aware, role-specific cover letters.

Tone matrix (enforced in system prompt):
  USA/Canada   → achievement-focused, confident, punchy, 250–300 words
  Germany      → formal, structured, third-person intro optional, 300–350 words
  Netherlands  → direct, no fluff, 200–250 words
  France       → formal, personal connection to institution, 300 words
  Sweden/EU    → concise, values-aligned, 250 words

On retry: improvement_notes from Verifier are injected as explicit fix instructions.
"""

from __future__ import annotations
import httpx
import re

from agents.base_agent import BaseAgent, with_retry
from models.schemas import JDAnalysis, MasterResume, Country


# ─── Tone profiles ────────────────────────────────────────────────────────────

TONE_PROFILES: dict[str, dict] = {
    Country.USA.value: {
        "style": "confident, achievement-focused, direct",
        "word_range": "250–300",
        "opener": "Lead with your strongest, most relevant achievement. No 'I am writing to apply'.",
        "structure": "Hook → 2 achievement paragraphs → Why this company → Call to action",
        "avoid": "passive voice, generic enthusiasm, phrases like 'I am passionate about'",
    },
    Country.CANADA.value: {
        "style": "professional, collaborative, achievement-focused",
        "word_range": "250–300",
        "opener": "Open with specific interest in the role or team.",
        "structure": "Interest → Relevant experience × 2 → Fit → Next steps",
        "avoid": "overconfidence, clichés",
    },
    Country.GERMANY.value: {
        "style": "formal, structured, respectful — German academic convention",
        "word_range": "300–350",
        "opener": "Formal salutation (Sehr geehrte Damen und Herren if name unknown). State full position title and reference number.",
        "structure": "Formal intro → Academic/research background → Technical skills → Motivation for this institution → Formal close",
        "avoid": "contractions, slang, casual tone, excessive enthusiasm",
        "note": "Include: full name, date, sender/recipient address block at top if address known",
    },
    Country.NETHERLANDS.value: {
        "style": "direct, no-nonsense, brief",
        "word_range": "200–250",
        "opener": "State what you want and why in the first sentence.",
        "structure": "Why this role → What you bring (2 concrete examples) → Brief close",
        "avoid": "flattery, long warm-up, vague statements",
    },
    Country.FRANCE.value: {
        "style": "formal, intellectual, personal connection to institution",
        "word_range": "300 words",
        "opener": "Formal salutation. Reference specific research or work of the lab.",
        "structure": "Formal intro → Academic background → Research synergy with lab → Availability and formalities",
        "avoid": "casual language, bullet points inside cover letter",
    },
    "default": {
        "style": "professional, clear, focused",
        "word_range": "250–300",
        "opener": "Open with your most relevant experience.",
        "structure": "Introduction → Relevant skills → Why this role → Close",
        "avoid": "clichés, vague claims",
    },
}


SYSTEM_PROMPT_TEMPLATE = """
You are an expert career coach specializing in internship applications for research roles.
Write a cover letter following EVERY instruction below. Output ONLY the cover letter text — no labels, no metadata.

TONE & STYLE ({country}):
- Style: {style}
- Target length: {word_range} words
- Opening instruction: {opener}
- Structure: {structure}
- AVOID: {avoid}

HARD RULES:
1. Every claim about the candidate's experience MUST come from the provided resume data.
2. Do not fabricate publications, projects, awards, or skills.
3. Reference the company/lab by name at least once.
4. Reference the specific role title.
5. Do not use: "I am passionate", "I am excited", "I would love to", "hardworking", "team player".
6. No bullet points inside the cover letter — flowing prose only.
7. Personalize using the company summary provided.

TOP 3 DIFFERENTIATORS TO HIGHLIGHT:
Scan the candidate's experience and the JD analysis. Identify the 3 strongest matches.
Weave these into the letter — do not list them, embed them in narrative.
"""


class CoverLetterAgent(BaseAgent):

    @with_retry(max_attempts=2)
    async def run(
        self,
        master: MasterResume,
        jd: JDAnalysis,
        improvement_notes: list[str] | None = None,
    ) -> str:
        """Returns cover letter as plain text."""
        company_info = await self._fetch_company_info(
            str(jd.listing.url),
            jd.listing.company,
        )
        system = self._build_system(jd)
        user = self._build_user(master, jd, company_info, improvement_notes or [])
        return await self._llm(system, user, max_tokens=1200)

    # ─── System prompt ────────────────────────────────────────────────────────

    def _build_system(self, jd: JDAnalysis) -> str:
        country = jd.listing.country.value
        profile = TONE_PROFILES.get(country, TONE_PROFILES["default"])
        return SYSTEM_PROMPT_TEMPLATE.format(
            country=country.upper(),
            style=profile["style"],
            word_range=profile["word_range"],
            opener=profile["opener"],
            structure=profile["structure"],
            avoid=profile["avoid"],
        )

    # ─── User message ─────────────────────────────────────────────────────────

    def _build_user(
        self,
        master: MasterResume,
        jd: JDAnalysis,
        company_info: str,
        improvement_notes: list[str],
    ) -> str:
        # Top experiences (last 3, most relevant)
        experience_summary = []
        for exp in master.experiences[:3]:
            experience_summary.append(
                f"{exp.title} @ {exp.company}: " + " | ".join(exp.bullets[:2])
            )

        parts = [
            f"CANDIDATE: {master.name}",
            f"ROLE APPLYING FOR: {jd.listing.title} at {jd.listing.company}",
            f"COUNTRY: {jd.listing.country.value.upper()}",
            "",
            "CANDIDATE SUMMARY:",
            f"  Skills: {', '.join(master.skills[:20])}",
            "  Key experiences:",
        ]
        for e in experience_summary:
            parts.append(f"    • {e}")

        if master.publications:
            parts.append(f"  Publications: {'; '.join(master.publications[:3])}")

        parts += [
            "",
            "JD REQUIRED KEYWORDS (reference naturally if candidate has them):",
            "  " + ", ".join(k.keyword for k in jd.keywords if k.required)[:300],
            "",
            "COMPANY / LAB SUMMARY (use this to personalize):",
            "  " + (company_info or jd.company_summary or "No additional info available.")[:600],
        ]

        if improvement_notes:
            parts += [
                "",
                "⚠ FIXES REQUIRED FROM PREVIOUS ATTEMPT:",
            ]
            parts.extend(f"  - {n}" for n in improvement_notes[:8])

        return "\n".join(parts)

    # ─── Live company fetch ───────────────────────────────────────────────────

    async def _fetch_company_info(self, job_url: str, company_name: str) -> str:
        """
        Best-effort: fetch job page and extract company blurb.
        Falls back to empty string — cover letter continues without it.
        """
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(
                    job_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; InternBot/1.0)"},
                )
                if resp.status_code != 200:
                    return ""
                text = resp.text
                # Naive extraction: grab meta description or first 600 chars of body text
                meta = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]{20,400})"', text)
                if meta:
                    return meta.group(1)
                # Strip tags and return first 400 chars of visible text
                stripped = re.sub(r"<[^>]+>", " ", text)
                stripped = re.sub(r"\s+", " ", stripped).strip()
                return stripped[:400]
        except Exception as e:
            self.log.warning("company_fetch_failed", company=company_name, error=str(e))
            return ""

    async def run(
        self,
        master: "MasterResume",
        jd: "JDAnalysis",
        improvement_notes: list[str] | None = None,
    ) -> str:
        company_info = await self._fetch_company_info(str(jd.listing.url), jd.listing.company)
        system = self._build_system(jd)
        user = self._build_user(master, jd, company_info, improvement_notes or [])
        return await self._llm(system, user, max_tokens=1200)
