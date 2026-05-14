"""
Shared Pydantic schemas — single source of truth for all agent I/O.
Every agent imports from here; nothing defines its own ad-hoc dicts.
"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, HttpUrl


# ─── Enums ───────────────────────────────────────────────────────────────────

class Country(str, Enum):
    USA = "usa"
    CANADA = "canada"
    GERMANY = "germany"
    NETHERLANDS = "netherlands"
    FRANCE = "france"
    UK = "uk"
    SWEDEN = "sweden"
    SWITZERLAND = "switzerland"
    AUSTRALIA = "australia"
    OTHER = "other"


class ApplicationStatus(str, Enum):
    QUEUED = "queued"
    GENERATING = "generating"
    VERIFYING = "verifying"
    PENDING_HUMAN = "pending_human"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    VIEWED = "viewed"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    ERROR = "error"


class ErrorSeverity(str, Enum):
    TRANSIENT = "transient"       # retry
    STRUCTURAL = "structural"     # portal changed layout
    CONTENT = "content"           # ATS never passes → human
    AUTH = "auth"                 # re-auth then escalate
    CRITICAL = "critical"         # halt pipeline


# ─── Resume schema ────────────────────────────────────────────────────────────

class ResumeExperience(BaseModel):
    title: str
    company: str
    start: str
    end: str | None = None
    bullets: list[str]
    skills_mentioned: list[str] = Field(default_factory=list)


class ResumeEducation(BaseModel):
    degree: str
    field: str
    institution: str
    gpa: float | None = None
    year: int


class MasterResume(BaseModel):
    """Parsed once at session start. Read-only for all downstream agents."""
    raw_text: str
    name: str
    email: str
    phone: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    summary: str | None = None
    skills: list[str]                          # flat normalized list
    experiences: list[ResumeExperience]
    education: list[ResumeEducation]
    publications: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


# ─── User preferences ─────────────────────────────────────────────────────────

class UserPrefs(BaseModel):
    target_countries: list[Country]
    target_domains: list[str]                  # ["ML research", "bioinformatics"]
    min_deadline_days: int = 7
    work_auth: dict[Country, bool] = Field(default_factory=dict)
    # analytics agent updates these weights each cycle
    country_weights: dict[Country, float] = Field(default_factory=dict)
    domain_weights: dict[str, float] = Field(default_factory=dict)
    max_concurrent_apps: int = 5
    dry_run: bool = True                       # safety: no real submit until flipped


# ─── Job listing ──────────────────────────────────────────────────────────────

class JobListing(BaseModel):
    id: str
    title: str
    company: str
    country: Country
    portal: str                                # "linkedin" | "daad" | "euraxess" | ...
    url: HttpUrl
    description: str
    deadline: datetime | None = None
    fit_score: float = 0.0                     # 0–1, set by Discovery Agent
    discovered_at: datetime = Field(default_factory=datetime.utcnow)


# ─── JD Analysis output ───────────────────────────────────────────────────────

class KeywordWeight(BaseModel):
    keyword: str
    weight: float                              # 0–1, higher = more critical
    required: bool                             # True = hard requirement
    section_origin: str                        # "title" | "requirements" | "preferred"


class CountryFormatRules(BaseModel):
    include_photo: bool = False
    include_address: bool = False
    max_pages: int = 1
    date_format: str = "MM/YYYY"               # "MM/YYYY" | "YYYY-MM" | etc.
    europass_format: bool = False
    funding_statement_required: bool = False   # DAAD, some EU grants
    cover_letter_tone: str = "professional"    # "formal" | "professional" | "direct"


class JDAnalysis(BaseModel):
    listing: JobListing
    keywords: list[KeywordWeight]
    required_skills: list[str]
    preferred_skills: list[str]
    format_rules: CountryFormatRules
    company_summary: str | None = None         # fetched live
    tone_signals: list[str] = Field(default_factory=list)


# ─── Verification output ──────────────────────────────────────────────────────

class ATSScoreReport(BaseModel):
    score: float                               # 0–100
    keyword_hits: dict[str, bool]              # keyword → matched?
    required_coverage: float                   # 0–1
    preferred_coverage: float                  # 0–1
    format_issues: list[str]
    improvement_notes: list[str]
    passed: bool                               # score >= threshold


class QualityReport(BaseModel):
    score: float                               # 0–10
    hallucination_flags: list[str]             # claims not in master resume
    tone_issues: list[str]
    grammar_issues: list[str]
    length_issue: str | None
    passed: bool                               # score >= 7.0 and no hallucinations


class VerificationResult(BaseModel):
    ats: ATSScoreReport
    quality: QualityReport
    overall_passed: bool
    retry_count: int = 0
    improvement_notes: list[str]               # merged, sent back to generator


# ─── Application record ───────────────────────────────────────────────────────

class ApplicationRecord(BaseModel):
    id: str
    listing: JobListing
    resume_path: str
    cover_letter_path: str
    verification: VerificationResult
    status: ApplicationStatus = ApplicationStatus.QUEUED
    confirmation_id: str | None = None
    submitted_at: datetime | None = None
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    response_received_at: datetime | None = None
    rejection_reason: str | None = None
    error_log: list[str] = Field(default_factory=list)
    retry_count: int = 0


# ─── Error event ──────────────────────────────────────────────────────────────

class PipelineError(BaseModel):
    application_id: str
    agent: str
    severity: ErrorSeverity
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = 0
    occurred_at: datetime = Field(default_factory=datetime.utcnow)
