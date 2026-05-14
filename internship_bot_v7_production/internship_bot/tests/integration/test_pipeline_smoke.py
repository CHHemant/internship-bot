"""
Integration smoke test — full pipeline dry run, no real submissions.
Uses mock listings (no network) + real LLM calls (needs ANTHROPIC_API_KEY).

Run: pytest tests/integration/test_pipeline_smoke.py -v -s
"""

from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from models.schemas import (
    ApplicationStatus,
    Country,
    CountryFormatRules,
    JDAnalysis,
    JobListing,
    KeywordWeight,
    MasterResume,
    ResumeEducation,
    ResumeExperience,
    UserPrefs,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def master_resume() -> MasterResume:
    return MasterResume(
        raw_text="Sample resume text for testing",
        name="Arjun Sharma",
        email="arjun@example.com",
        phone="+91-9876543210",
        linkedin="https://linkedin.com/in/arjunsharma",
        github="https://github.com/arjunsharma",
        summary="ML researcher with 2 years experience in deep learning and NLP.",
        skills=[
            "python", "pytorch", "machine learning", "deep learning",
            "nlp", "transformers", "git", "linux", "sql", "latex",
        ],
        experiences=[
            ResumeExperience(
                title="Research Assistant",
                company="IIT Bombay AI Lab",
                start="2023",
                end="2024",
                bullets=[
                    "Developed transformer-based NLP model achieving 94% accuracy on benchmark",
                    "Published paper on attention mechanisms at NeurIPS workshop",
                    "Implemented data pipeline processing 50GB text corpus using Python and SQL",
                ],
                skills_mentioned=["python", "pytorch", "nlp", "transformers"],
            )
        ],
        education=[
            ResumeEducation(
                degree="B.Tech",
                field="Computer Science",
                institution="IIT Bombay",
                gpa=9.1,
                year=2025,
            )
        ],
        publications=["Sharma et al. (2024). Efficient Attention Mechanisms. NeurIPS Workshop."],
        certifications=[],
        languages=["English (fluent)", "Hindi (native)"],
    )


@pytest.fixture
def user_prefs() -> UserPrefs:
    return UserPrefs(
        target_countries=[Country.USA, Country.GERMANY, Country.CANADA],
        target_domains=["ML research", "NLP", "deep learning"],
        min_deadline_days=7,
        work_auth={Country.USA: False, Country.GERMANY: False, Country.CANADA: False},
        max_concurrent_apps=2,
        dry_run=True,  # CRITICAL — no real submissions in tests
    )


@pytest.fixture
def mock_listing() -> JobListing:
    return JobListing(
        id="test-listing-001",
        title="Machine Learning Research Intern",
        company="MIT CSAIL",
        country=Country.USA,
        portal="handshake",
        url="https://app.joinhandshake.com/jobs/12345",
        description=(
            "We are seeking a research intern with experience in "
            "python, pytorch, machine learning, deep learning, and nlp. "
            "Required: transformers, git. Preferred: docker, cuda. "
            "Join our group to work on large language models and NLP research."
        ),
        deadline=datetime.now(timezone.utc) + timedelta(days=45),
        fit_score=0.82,
    )


@pytest.fixture
def mock_jd_analysis(mock_listing) -> JDAnalysis:
    return JDAnalysis(
        listing=mock_listing,
        keywords=[
            KeywordWeight(keyword="python",           weight=0.9, required=True,  section_origin="requirements"),
            KeywordWeight(keyword="pytorch",           weight=0.9, required=True,  section_origin="requirements"),
            KeywordWeight(keyword="machine learning",  weight=0.9, required=True,  section_origin="title"),
            KeywordWeight(keyword="deep learning",     weight=0.8, required=True,  section_origin="requirements"),
            KeywordWeight(keyword="nlp",               weight=0.8, required=True,  section_origin="requirements"),
            KeywordWeight(keyword="transformers",      weight=0.7, required=True,  section_origin="requirements"),
            KeywordWeight(keyword="git",               weight=0.6, required=True,  section_origin="requirements"),
            KeywordWeight(keyword="docker",            weight=0.5, required=False, section_origin="preferred"),
            KeywordWeight(keyword="cuda",              weight=0.5, required=False, section_origin="preferred"),
            KeywordWeight(keyword="large language models", weight=0.7, required=False, section_origin="description"),
        ],
        required_skills=["python", "pytorch", "machine learning", "deep learning", "nlp", "transformers", "git"],
        preferred_skills=["docker", "cuda", "large language models"],
        format_rules=CountryFormatRules(
            include_photo=False, include_address=False, max_pages=1,
        ),
        company_summary="MIT CSAIL is one of the world's leading AI research labs.",
        tone_signals=["professional", "research-focused"],
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestATSScorerIntegration:
    """ATS scorer on realistic resume + JD."""

    def test_good_resume_passes(self, master_resume, mock_jd_analysis):
        from agents.verifier.ats_scorer import ATSScorer
        scorer = ATSScorer()
        # Build resume text from master resume
        resume_text = (
            f"{master_resume.name}\n{master_resume.email}\n"
            f"Skills: {', '.join(master_resume.skills)}\n"
            "Experience\nEducation\n"
        )
        for exp in master_resume.experiences:
            resume_text += f"\n{exp.title} at {exp.company}\n"
            resume_text += "\n".join(exp.bullets)

        report = scorer.score(resume_text, mock_jd_analysis)
        assert report.passed, f"Expected pass, got score={report.score}, notes={report.improvement_notes}"
        assert report.required_coverage >= 0.8

    def test_empty_resume_fails(self, mock_jd_analysis):
        from agents.verifier.ats_scorer import ATSScorer
        scorer = ATSScorer()
        report = scorer.score("", mock_jd_analysis)
        assert not report.passed
        assert report.score < 70.0


class TestResumeCustomizerSmoke:
    """Resume Customizer produces non-empty output with required keywords."""

    @pytest.mark.asyncio
    async def test_generates_resume(self, master_resume, mock_jd_analysis):
        from agents.resume_customizer.agent import ResumeCustomizerAgent
        agent = ResumeCustomizerAgent()
        result = await agent.run(master_resume, mock_jd_analysis)
        assert len(result) > 200, "Resume output too short"
        # Should contain candidate's name
        assert master_resume.name.split()[0].lower() in result.lower()

    @pytest.mark.asyncio
    async def test_no_hallucination_in_output(self, master_resume, mock_jd_analysis):
        from agents.resume_customizer.agent import ResumeCustomizerAgent
        agent = ResumeCustomizerAgent()
        result = await agent.run(master_resume, mock_jd_analysis)
        # Should NOT contain skills not in master resume
        assert "kubernetes" not in result.lower()  # not in master resume


class TestVerificationPipelineSmoke:
    """End-to-end verification on a well-matched resume."""

    @pytest.mark.asyncio
    async def test_good_resume_passes_verification(self, master_resume, mock_jd_analysis):
        from agents.verifier import VerificationSuite
        from agents.resume_customizer.agent import ResumeCustomizerAgent

        # Generate resume
        gen = ResumeCustomizerAgent()
        resume_text = await gen.run(master_resume, mock_jd_analysis)

        # Verify
        verifier = VerificationSuite()
        result = await verifier.run(
            resume_text=resume_text,
            cover_letter_text="Sample cover letter",
            jd_analysis=mock_jd_analysis,
            master_resume=master_resume,
        )
        # With a well-matched resume, should pass ATS at minimum
        assert result.ats.score > 50.0, f"ATS score too low: {result.ats.score}"


class TestCoverLetterSmoke:
    """Cover letter agent produces country-appropriate output."""

    @pytest.mark.asyncio
    async def test_usa_cover_letter(self, master_resume, mock_jd_analysis):
        from agents.cover_letter.agent import CoverLetterAgent
        agent = CoverLetterAgent()
        result = await agent.run(master_resume, mock_jd_analysis)
        assert len(result) > 100
        assert "MIT" in result or "research" in result.lower()

    @pytest.mark.asyncio
    async def test_cover_letter_no_fabrication(self, master_resume, mock_jd_analysis):
        from agents.cover_letter.agent import CoverLetterAgent
        agent = CoverLetterAgent()
        result = await agent.run(master_resume, mock_jd_analysis)
        # Should not claim skills not in resume
        assert "kubernetes" not in result.lower()


class TestPortalDetection:
    """Router correctly identifies portal from URL."""

    def test_handshake_detected(self):
        from agents.router.portal_configs import detect_portal, PortalType
        config = detect_portal("https://app.joinhandshake.com/jobs/12345")
        assert config.type == PortalType.HANDSHAKE

    def test_daad_detected(self):
        from agents.router.portal_configs import detect_portal, PortalType
        config = detect_portal("https://www.daad.de/en/study-and-research/")
        assert config.type == PortalType.DAAD

    def test_euraxess_detected(self):
        from agents.router.portal_configs import detect_portal, PortalType
        config = detect_portal("https://euraxess.ec.europa.eu/jobs/12345")
        assert config.type == PortalType.EURAXESS

    def test_unknown_portal_returns_generic(self):
        from agents.router.portal_configs import detect_portal, PortalType
        config = detect_portal("https://totally-unknown-university-portal.edu/apply")
        assert config.type in (PortalType.UNIVERSITY, PortalType.GENERIC)


class TestSecurityVault:
    """Vault encrypts and decrypts correctly."""

    def test_encrypt_decrypt_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VAULT_MASTER_PASSWORD", "test-password-12345")
        from security.vault import CredentialVault
        vault = CredentialVault.open()
        vault.store("test_portal", "user@test.com", "secret123")
        email, pwd = vault.retrieve("test_portal")
        assert email == "user@test.com"
        assert pwd == "secret123"

    def test_wrong_password_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VAULT_MASTER_PASSWORD", "correct-password")
        from security.vault import CredentialVault
        vault = CredentialVault.open()
        vault.store("test_portal", "user@test.com", "secret")
        # Now try wrong password
        monkeypatch.setenv("VAULT_MASTER_PASSWORD", "wrong-password")
        with pytest.raises(Exception):
            CredentialVault.open()
