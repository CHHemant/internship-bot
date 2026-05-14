"""
Quality Reviewer — Layer 2 of the Verification Suite.
Uses LLM to check what ATS scorer can't: hallucinations, tone, grammar.

Two concerns:
  1. Hallucination guard: no claim in the custom resume should be absent
     from the master resume. We diff them explicitly.
  2. Tone + grammar: light-touch LLM review.

No fuzzy logic here — any hallucination = hard fail, no exceptions.
"""

from __future__ import annotations
import json
import re

from agents.base_agent import BaseAgent
from models.schemas import MasterResume, QualityReport


# Minimum quality score to pass (0–10 scale)
QUALITY_THRESHOLD = 7.0

SYSTEM_PROMPT = """
You are a strict resume quality auditor. Your ONLY job is to output valid JSON.
Do not add any commentary outside the JSON object.

You will receive:
  - master_skills: the original skills list from the candidate's master resume
  - master_experience_bullets: all bullet points from the original resume
  - custom_resume_text: the AI-generated custom resume to audit

Your task — detect THREE things:

1. HALLUCINATIONS: Any skill, technology, achievement, or claim in the custom
   resume that is NOT present (or reasonably inferable) from the master resume.
   Be strict: "led a team of 10" when no leadership is mentioned = hallucination.
   "Python" when Python appears in master = fine.

2. TONE ISSUES: Overuse of clichés ("passionate", "guru", "ninja", "rockstar"),
   first-person pronouns (I/me/my in bullet points), weak verbs (helped, assisted,
   worked on), or inappropriate casualness.

3. GRAMMAR / LENGTH ISSUES: Obvious grammar errors, incomplete sentences,
   resume longer than declared max_pages (if provided).

Output ONLY this JSON (no markdown fences, no extra text):
{
  "hallucination_flags": ["<specific claim> — not found in master resume"],
  "tone_issues": ["<specific issue>"],
  "grammar_issues": ["<specific issue>"],
  "length_issue": "<issue string or null>",
  "score": <float 0.0–10.0>,
  "reasoning": "<one sentence>"
}

Scoring guide:
  10.0 = perfect, no issues
  8.0–9.9 = minor tone/grammar, no hallucinations
  7.0–7.9 = several tone issues but no fabricated claims
  < 7.0 = hallucinations present OR multiple serious issues
  Any hallucination → score must be <= 4.0
"""


class QualityReviewer(BaseAgent):
    """LLM-powered accuracy and tone reviewer."""

    async def review(
        self,
        custom_resume_text: str,
        master_resume: MasterResume,
        max_pages: int = 1,
    ) -> QualityReport:

        master_bullets = []
        for exp in master_resume.experiences:
            master_bullets.extend(exp.bullets)

        user_msg = json.dumps(
            {
                "master_skills": master_resume.skills,
                "master_experience_bullets": master_bullets[:40],  # token budget
                "custom_resume_text": custom_resume_text[:6000],   # token budget
                "max_pages": max_pages,
            },
            ensure_ascii=False,
        )

        raw = await self._llm(SYSTEM_PROMPT, user_msg, max_tokens=1024)
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> QualityReport:
        """Parse LLM JSON output into QualityReport. Defensive."""
        try:
            # Strip any accidental markdown fences
            clean = re.sub(r"```(?:json)?|```", "", raw).strip()
            data = json.loads(clean)
            score = float(data.get("score", 5.0))
            hallucinations = data.get("hallucination_flags", [])
            return QualityReport(
                score=score,
                hallucination_flags=hallucinations,
                tone_issues=data.get("tone_issues", []),
                grammar_issues=data.get("grammar_issues", []),
                length_issue=data.get("length_issue"),
                # Fail if any hallucination OR score below threshold
                passed=(score >= QUALITY_THRESHOLD and len(hallucinations) == 0),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            # Parse failure → conservative fail
            return QualityReport(
                score=0.0,
                hallucination_flags=["Quality review parse failure — treating as fail"],
                tone_issues=[],
                grammar_issues=[],
                length_issue=None,
                passed=False,
            )

    async def run(self, *args, **kwargs) -> QualityReport:
        return await self.review(*args, **kwargs)
