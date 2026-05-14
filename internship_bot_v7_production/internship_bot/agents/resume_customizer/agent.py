"""
Resume Customizer Agent — rewrites master resume for a specific JD.

Core rules (enforced in system prompt):
  1. Never fabricate skills not in master resume
  2. Inject high-weight keywords naturally into bullet points
  3. Apply country format rules (length, sections, photo flag)
  4. On retry: improvement_notes from Verifier become explicit instructions
"""

from __future__ import annotations
from agents.base_agent import BaseAgent, with_retry
from models.schemas import JDAnalysis, MasterResume


SYSTEM_PROMPT_TEMPLATE = """
You are an expert resume writer and ATS optimization specialist.
Your output is ONLY the final resume text — no commentary, no markdown headers outside sections.

HARD RULES:
1. NEVER add skills, experiences, tools, or achievements not present in the master resume.
2. All facts must be directly supported by the provided master resume data.
3. Do not use first-person pronouns in bullet points (no I/me/my).
4. Avoid clichés: "passionate", "guru", "rockstar", "ninja", "hardworking".
5. Start every bullet with a strong action verb (Built, Developed, Designed, Analyzed, Published...).
6. Include EVERY keyword from the required_keywords list naturally — do not keyword-stuff.

FORMAT RULES for {country}:
- Max pages: {max_pages}
- Include photo placeholder: {include_photo}
- Include full address: {include_address}
- Date format: {date_format}
- Europass: {europass_format}

SECTIONS ORDER:
{sections}

TARGET JD ANALYSIS:
- Required keywords (MUST appear): {required_keywords}
- Preferred keywords (include where natural): {preferred_keywords}
- Role: {role_title} at {company}
"""


class ResumeCustomizerAgent(BaseAgent):

    @with_retry(max_attempts=2)
    async def run(
        self,
        master: MasterResume,
        jd: JDAnalysis,
        improvement_notes: list[str] | None = None,
    ) -> str:
        """Returns the full custom resume as plain text."""
        system = self._build_system(jd)
        user = self._build_user(master, jd, improvement_notes or [])
        return await self._llm(system, user, max_tokens=3000)

    def _build_system(self, jd: JDAnalysis) -> str:
        fmt = jd.format_rules
        sections = self._sections_order(jd.listing.country.value)
        required_kw = ", ".join(
            k.keyword for k in jd.keywords if k.required
        )[:500]
        preferred_kw = ", ".join(
            k.keyword for k in jd.keywords if not k.required
        )[:300]

        return SYSTEM_PROMPT_TEMPLATE.format(
            country=jd.listing.country.value.upper(),
            max_pages=fmt.max_pages,
            include_photo=fmt.include_photo,
            include_address=fmt.include_address,
            date_format=fmt.date_format,
            europass_format=fmt.europass_format,
            sections=sections,
            required_keywords=required_kw,
            preferred_keywords=preferred_kw,
            role_title=jd.listing.title,
            company=jd.listing.company,
        )

    def _build_user(
        self,
        master: MasterResume,
        jd: JDAnalysis,
        improvement_notes: list[str],
    ) -> str:
        parts = [
            f"CANDIDATE: {master.name}",
            f"EMAIL: {master.email}",
            f"SKILLS: {', '.join(master.skills)}",
            "\nEXPERIENCE:",
        ]
        for exp in master.experiences:
            parts.append(f"  {exp.title} at {exp.company} ({exp.start}–{exp.end or 'Present'})")
            for b in exp.bullets:
                parts.append(f"    • {b}")

        parts.append("\nEDUCATION:")
        for edu in master.education:
            parts.append(f"  {edu.degree} in {edu.field}, {edu.institution} ({edu.year})")

        if master.publications:
            parts.append("\nPUBLICATIONS:")
            parts.extend(f"  • {p}" for p in master.publications)

        if improvement_notes:
            parts.append("\n⚠ IMPROVEMENT NOTES FROM PREVIOUS ATTEMPT (fix these):")
            parts.extend(f"  - {note}" for note in improvement_notes[:10])

        return "\n".join(parts)

    @staticmethod
    def _sections_order(country: str) -> str:
        orders = {
            "germany": "1.Contact Info + Photo  2.Summary  3.Education  4.Experience  5.Skills  6.Publications  7.Languages",
            "usa":     "1.Contact Info  2.Summary  3.Experience  4.Skills  5.Education  6.Publications",
            "canada":  "1.Contact Info  2.Summary  3.Experience  4.Skills  5.Education",
        }
        return orders.get(country, "1.Contact Info  2.Summary  3.Experience  4.Skills  5.Education")
