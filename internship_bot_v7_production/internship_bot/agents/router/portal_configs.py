"""
Portal configuration registry.

Each PortalConfig describes HOW to interact with one portal type:
  - URL pattern to identify the portal
  - Auth strategy
  - Required extra documents
  - Field mapping hints for the form filler
  - Known CAPTCHA behaviour

Add new portals by adding a PortalConfig to PORTAL_REGISTRY.
The router selects the first config whose url_patterns match the listing URL.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import re


class AuthStrategy(str, Enum):
    OAUTH_LINKEDIN = "oauth_linkedin"      # LinkedIn SSO
    EMAIL_PASSWORD = "email_password"      # standard form login
    SSO_UNIVERSITY = "sso_university"      # Shibboleth / institution SSO
    NO_AUTH = "no_auth"                    # direct apply, no account needed
    API_TOKEN = "api_token"                # Handshake API


class PortalType(str, Enum):
    LINKEDIN = "linkedin"
    DAAD = "daad"
    EURAXESS = "euraxess"
    HANDSHAKE = "handshake"
    UNIVERSITY = "university"
    GENERIC = "generic"


@dataclass
class PortalConfig:
    type: PortalType
    url_patterns: list[str]                        # regex patterns matched against listing URL
    auth_strategy: AuthStrategy
    supports_easy_apply: bool = False              # one-click apply possible?
    requires_cover_letter_upload: bool = True
    requires_resume_upload: bool = True
    extra_fields: list[str] = field(default_factory=list)   # extra form fields required
    extra_documents: list[str] = field(default_factory=list)  # e.g. funding statement
    captcha_expected: bool = False
    max_file_size_mb: int = 5
    accepted_formats: list[str] = field(default_factory=lambda: ["pdf"])
    notes: str = ""


# ─── Registry ─────────────────────────────────────────────────────────────────

PORTAL_REGISTRY: list[PortalConfig] = [

    PortalConfig(
        type=PortalType.LINKEDIN,
        url_patterns=[r"linkedin\.com/jobs", r"linkedin\.com/in/"],
        auth_strategy=AuthStrategy.OAUTH_LINKEDIN,
        supports_easy_apply=True,
        requires_cover_letter_upload=False,    # Easy Apply: optional upload
        extra_fields=["phone", "years_of_experience", "work_authorization"],
        captcha_expected=True,
        notes="Easy Apply fills most fields from profile. Non-Easy Apply → full form.",
    ),

    PortalConfig(
        type=PortalType.DAAD,
        url_patterns=[r"daad\.de", r"scholarship-database\.daad\.de"],
        auth_strategy=AuthStrategy.EMAIL_PASSWORD,
        supports_easy_apply=False,
        extra_fields=["nationality", "language_certificates", "motivation_statement"],
        extra_documents=["funding_statement", "language_certificate", "academic_transcript"],
        max_file_size_mb=10,
        accepted_formats=["pdf"],
        notes=(
            "DAAD requires a separate Bewerbungsportal account. "
            "Funding statement is mandatory — include research plan (1–2 pages). "
            "Date format: DD.MM.YYYY throughout."
        ),
    ),

    PortalConfig(
        type=PortalType.EURAXESS,
        url_patterns=[r"euraxess\.ec\.europa\.eu", r"euraxess\."],
        auth_strategy=AuthStrategy.EMAIL_PASSWORD,
        supports_easy_apply=False,
        extra_fields=["nationality", "research_field_code", "career_stage"],
        extra_documents=["europass_cv", "motivation_letter", "recommendation_letters"],
        max_file_size_mb=5,
        accepted_formats=["pdf"],
        notes=(
            "Europass CV format strongly preferred. "
            "Research field must match EURAXESS taxonomy code. "
            "Two recommendation letter uploads typically required."
        ),
    ),

    PortalConfig(
        type=PortalType.HANDSHAKE,
        url_patterns=[r"joinhandshake\.com", r"app\.joinhandshake\.com"],
        auth_strategy=AuthStrategy.EMAIL_PASSWORD,
        supports_easy_apply=True,
        extra_fields=["graduation_date", "gpa", "major", "work_authorization"],
        captcha_expected=False,
        notes=(
            "Handshake is USA/Canada focused. University email required. "
            "GPA field is standard. Work auth dropdown: F-1 OPT / CPT / None."
        ),
    ),

    # Generic university career portal (fallback for direct university portals)
    PortalConfig(
        type=PortalType.UNIVERSITY,
        url_patterns=[r"\.edu/", r"\.ac\.uk/", r"\.uni-", r"careers\.", r"jobs\."],
        auth_strategy=AuthStrategy.EMAIL_PASSWORD,
        supports_easy_apply=False,
        extra_fields=["cover_letter_text"],    # many university portals have a text box
        extra_documents=["academic_transcript", "recommendation_letters"],
        captcha_expected=True,
        notes=(
            "University portals vary widely. Form filler uses generic selectors with fallback. "
            "Manual review recommended before submission."
        ),
    ),

    # Catch-all generic portal
    PortalConfig(
        type=PortalType.GENERIC,
        url_patterns=[r".*"],
        auth_strategy=AuthStrategy.EMAIL_PASSWORD,
        supports_easy_apply=False,
        captcha_expected=True,
        notes="Unknown portal. Generic selectors applied. High probability of human escalation.",
    ),
]


def detect_portal(url: str) -> PortalConfig:
    """Match URL against registry. First match wins (ordered by specificity)."""
    for config in PORTAL_REGISTRY:
        for pattern in config.url_patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return config
    return PORTAL_REGISTRY[-1]  # always returns at least GENERIC
