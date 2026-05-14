"""
Tests for ATSScorer.
Run: pytest tests/unit/test_ats_scorer.py -v
"""

from __future__ import annotations
import pytest
from unittest.mock import MagicMock
from datetime import datetime

from agents.verifier.ats_scorer import ATSScorer
from models.schemas import (
    Country,
    CountryFormatRules,
    JDAnalysis,
    JobListing,
    KeywordWeight,
)


def _make_jd(keywords: list[tuple[str, float, bool]]) -> JDAnalysis:
    """Helper: build a minimal JDAnalysis for testing."""
    kw_list = [
        KeywordWeight(keyword=k, weight=w, required=r, section_origin="requirements")
        for k, w, r in keywords
    ]
    listing = JobListing(
        id="test-001",
        title="Research Intern",
        company="Test Lab",
        country=Country.USA,
        portal="linkedin",
        url="https://example.com/job/1",
        description="test",
        deadline=datetime(2025, 12, 31),
    )
    return JDAnalysis(
        listing=listing,
        keywords=kw_list,
        required_skills=[k for k, _, r in keywords if r],
        preferred_skills=[k for k, _, r in keywords if not r],
        format_rules=CountryFormatRules(max_pages=1, include_photo=False),
    )


class TestKeywordMatching:

    scorer = ATSScorer()

    def test_exact_match_passes(self):
        jd = _make_jd([("python", 0.9, True)])
        resume = "Developed data pipelines using python and pandas."
        report = self.scorer.score(resume, jd)
        assert report.keyword_hits["python"] is True
        assert report.required_coverage == 1.0

    def test_synonym_match_pytorch_torch(self):
        jd = _make_jd([("pytorch", 0.9, True)])
        resume = "Built CNN models in torch for image classification."
        report = self.scorer.score(resume, jd)
        assert report.keyword_hits["pytorch"] is True

    def test_stemmed_match_research_researching(self):
        jd = _make_jd([("research", 0.8, True)])
        resume = "Spent 2 years researching protein folding mechanisms."
        report = self.scorer.score(resume, jd)
        assert report.keyword_hits["research"] is True

    def test_fuzzy_match_near_miss(self):
        jd = _make_jd([("natural language processing", 0.9, True)])
        resume = "Experience in natural-language processing tasks."
        report = self.scorer.score(resume, jd)
        assert report.keyword_hits["natural language processing"] is True

    def test_no_match_returns_false(self):
        jd = _make_jd([("kubernetes", 0.8, True)])
        resume = "Developed web apps using Django and PostgreSQL."
        report = self.scorer.score(resume, jd)
        assert report.keyword_hits["kubernetes"] is False
        assert report.required_coverage == 0.0

    def test_low_required_coverage_fails(self):
        """Resume missing all required keywords must score below threshold."""
        keywords = [(f"skill_{i}", 0.9, True) for i in range(5)]
        jd = _make_jd(keywords)
        resume = "I enjoy hiking and cooking Italian food."
        report = self.scorer.score(resume, jd)
        assert report.passed is False
        assert report.score < 70.0


class TestScoringFormula:

    scorer = ATSScorer()

    def test_full_required_coverage_no_preferred_scores_high(self):
        """60 pts from required + 25 from preferred (none) + 15 format = high."""
        jd = _make_jd([("python", 0.9, True), ("machine learning", 0.9, True)])
        resume = "Proficient in python and machine learning for data science projects."
        report = self.scorer.score(resume, jd)
        # required 100% → 60, preferred 100% (empty = 1.0) → 25, format → ~15
        assert report.score >= 70.0
        assert report.passed is True

    def test_partial_preferred_doesnt_fail_if_required_met(self):
        jd = _make_jd([
            ("python", 0.9, True),
            ("tensorflow", 0.5, False),
            ("docker", 0.4, False),
        ])
        resume = "Expert in python programming. No docker or cloud experience."
        report = self.scorer.score(resume, jd)
        # required 100% → 60, preferred 0/2 → 0, format → 15 = 75 → pass
        assert report.required_coverage == 1.0
        assert report.passed is True

    def test_missing_required_note_generated(self):
        jd = _make_jd([("pytorch", 0.9, True), ("cuda", 0.9, True)])
        resume = "I study computer science."
        report = self.scorer.score(resume, jd)
        assert any("CRITICAL" in note for note in report.improvement_notes)
        assert report.passed is False


class TestFormatScoring:

    scorer = ATSScorer()

    def test_tab_heavy_resume_penalized(self):
        jd = _make_jd([("python", 0.9, True)])
        # Simulate tabular layout
        resume = (
            "python\t•\tExpert\t5 years\n" * 30 +
            "Experience\nEducation\nSkills\ntest@example.com"
        )
        report = self.scorer.score(resume, jd, resume_page_count=1)
        assert any("table" in issue.lower() or "column" in issue.lower()
                   for issue in report.format_issues)

    def test_too_many_pages_penalized(self):
        jd = _make_jd([("python", 0.9, True)])
        resume = (
            "Experience\nEducation\nSkills\ntest@example.com\n"
            "python skills\n" * 20
        )
        report = self.scorer.score(resume, jd, resume_page_count=3)
        assert any("page" in issue.lower() for issue in report.format_issues)

    def test_missing_section_headers_flagged(self):
        jd = _make_jd([("python", 0.9, True)])
        resume = "John Doe. test@test.com. python developer for 5 years."
        report = self.scorer.score(resume, jd)
        assert any("section" in issue.lower() for issue in report.format_issues)

    def test_no_email_flagged(self):
        jd = _make_jd([("python", 0.9, True)])
        resume = "Experience\nEducation\nSkills\npython developer."
        report = self.scorer.score(resume, jd)
        assert any("email" in issue.lower() for issue in report.format_issues)


class TestEdgeCases:

    scorer = ATSScorer()

    def test_empty_resume_fails_gracefully(self):
        jd = _make_jd([("python", 0.9, True)])
        report = self.scorer.score("", jd)
        assert report.passed is False
        assert report.score < 70.0

    def test_empty_keyword_list_passes_format_only(self):
        jd = _make_jd([])
        resume = "Experience\nEducation\nSkills\ntest@example.com\nPython developer."
        report = self.scorer.score(resume, jd)
        # no keywords → required=1.0, preferred=1.0, format ok → score ~85+
        assert report.passed is True

    def test_keyword_stuffing_does_not_boost_score(self):
        """Repeating a keyword 20× shouldn't give extra score over 1×."""
        jd = _make_jd([("python", 0.9, True)])
        resume_once = "python Experience Education Skills test@example.com"
        resume_stuffed = "python " * 25 + " Experience Education Skills test@example.com"
        r1 = self.scorer.score(resume_once, jd)
        r2 = self.scorer.score(resume_stuffed, jd)
        assert abs(r1.score - r2.score) < 5.0  # stuffing gives no meaningful boost
