"""
Verification Suite — the gatekeeper.

Flow:
  1. Run ATS Scorer (deterministic, fast)
  2. If ATS passes: run Quality Reviewer (LLM, slower)
  3. On fail: merge improvement notes → return to generator
  4. Max retries: 3. After that → PENDING_HUMAN

Both layers must pass. ATS gate first (cheap). Quality gate second (expensive).
"""

from __future__ import annotations
import asyncio

import structlog

from agents.base_agent import BaseAgent
from agents.verifier.ats_scorer import ATSScorer
from agents.verifier.quality_reviewer import QualityReviewer
from models.schemas import (
    ErrorSeverity,
    JDAnalysis,
    MasterResume,
    VerificationResult,
)

MAX_RETRIES = 3
log = structlog.get_logger()


class VerificationSuite(BaseAgent):
    """
    Stateless gatekeeper. Returns VerificationResult with pass/fail + notes.
    Caller (orchestrator) handles retry logic using improvement_notes.
    """

    def __init__(self, error_bus=None):
        super().__init__(error_bus)
        self._ats = ATSScorer()
        self._quality = QualityReviewer(error_bus)

    async def run(
        self,
        resume_text: str,
        cover_letter_text: str,   # reserved for future cover letter ATS check
        jd_analysis: JDAnalysis,
        master_resume: MasterResume,
        resume_page_count: int = 1,
        retry_count: int = 0,
    ) -> VerificationResult:

        application_id = jd_analysis.listing.id
        log.info(
            "verification_start",
            application_id=application_id,
            retry=retry_count,
        )

        # ── Layer 1: ATS Score (no LLM, fast) ────────────────────────────────
        ats_report = self._ats.score(resume_text, jd_analysis, resume_page_count)

        log.info(
            "ats_scored",
            application_id=application_id,
            score=ats_report.score,
            required_coverage=ats_report.required_coverage,
            passed=ats_report.passed,
        )

        if not ats_report.passed:
            log.warning(
                "ats_failed",
                application_id=application_id,
                score=ats_report.score,
                notes=ats_report.improvement_notes[:3],
            )
            return VerificationResult(
                ats=ats_report,
                quality=_empty_quality(),
                overall_passed=False,
                retry_count=retry_count,
                improvement_notes=ats_report.improvement_notes,
            )

        # ── Layer 2: Quality Review (LLM) ─────────────────────────────────────
        quality_report = await self._quality.review(
            custom_resume_text=resume_text,
            master_resume=master_resume,
            max_pages=jd_analysis.format_rules.max_pages,
        )

        log.info(
            "quality_reviewed",
            application_id=application_id,
            score=quality_report.score,
            hallucinations=len(quality_report.hallucination_flags),
            passed=quality_report.passed,
        )

        overall_passed = ats_report.passed and quality_report.passed

        # Merge improvement notes from both layers
        all_notes: list[str] = []
        if not ats_report.passed:
            all_notes.extend(ats_report.improvement_notes)
        if not quality_report.passed:
            if quality_report.hallucination_flags:
                all_notes.append("REMOVE fabricated claims: " + "; ".join(quality_report.hallucination_flags))
            all_notes.extend(quality_report.tone_issues)
            all_notes.extend(quality_report.grammar_issues)
            if quality_report.length_issue:
                all_notes.append(quality_report.length_issue)

        # Escalate to human if unrecoverable (hallucinations after retries)
        if not overall_passed and retry_count >= MAX_RETRIES - 1:
            await self._emit_error(
                application_id=application_id,
                severity=ErrorSeverity.CONTENT,
                message=f"Verification failed after {retry_count + 1} attempts.",
                context={"notes": all_notes, "ats_score": ats_report.score},
            )

        result = VerificationResult(
            ats=ats_report,
            quality=quality_report,
            overall_passed=overall_passed,
            retry_count=retry_count,
            improvement_notes=all_notes,
        )

        log.info(
            "verification_complete",
            application_id=application_id,
            overall_passed=overall_passed,
            retry=retry_count,
        )
        return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _empty_quality():
    """Placeholder quality report when ATS fails early (skip LLM call)."""
    from models.schemas import QualityReport
    return QualityReport(
        score=0.0,
        hallucination_flags=[],
        tone_issues=[],
        grammar_issues=[],
        length_issue=None,
        passed=False,
    )
