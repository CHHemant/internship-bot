"""
Resume Parser — converts any resume file → MasterResume Pydantic schema.

Strategy:
  1. Extract raw text (PDF via pdfminer, DOCX via python-docx, TXT direct)
  2. LLM structured extraction → JSON matching MasterResume schema
  3. Validate with Pydantic; fall back to regex extraction on parse error

Why LLM extraction over pure regex:
  Resumes have wildly inconsistent formats. LLM handles:
  - Dates in any format
  - Non-standard section headers
  - Multilingual resumes (German/French candidates)
  - Skills embedded in prose vs bulleted lists

Security: raw text is processed in-memory only. Never written to disk unencrypted.
"""

from __future__ import annotations
import json
import re
from pathlib import Path

import structlog

from models.schemas import (
    MasterResume,
    ResumeEducation,
    ResumeExperience,
)

log = structlog.get_logger()

PARSE_SYSTEM_PROMPT = """
You are a resume parser. Extract ALL information from the resume text provided.
Output ONLY valid JSON — no markdown fences, no commentary.

Required schema:
{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "+1-555-555-5555 or null",
  "linkedin": "https://linkedin.com/in/... or null",
  "github": "https://github.com/... or null",
  "portfolio": "URL or null",
  "summary": "professional summary text or null",
  "skills": ["python", "pytorch", "machine learning", ...],
  "experiences": [
    {
      "title": "Research Intern",
      "company": "MIT CSAIL",
      "start": "2023",
      "end": "2024 or null if current",
      "bullets": ["Developed X", "Published Y", ...],
      "skills_mentioned": ["python", "pytorch"]
    }
  ],
  "education": [
    {
      "degree": "B.Tech",
      "field": "Computer Science",
      "institution": "IIT Bombay",
      "gpa": 9.2,
      "year": 2025
    }
  ],
  "publications": ["Paper title, venue, year"],
  "certifications": ["AWS Certified ML Specialty"],
  "languages": ["English (fluent)", "German (B2)"]
}

Rules:
- skills: flat list, lowercase, normalized (not "Python 3.11" → "python")
- bullets: clean, no leading bullet characters
- gpa: float or null (normalize to 4.0 scale if CGPA given on 10.0 scale by dividing by 2.5)
- year in education: graduation year as int
- If a field is missing: use null (not empty string)
- Extract ALL skills mentioned anywhere in the resume, including from bullets
"""


async def parse_resume(path: Path) -> MasterResume:
    """
    Main entry. Accepts PDF, DOCX, or TXT.
    Returns fully validated MasterResume.
    """
    log.info("parsing_resume", path=str(path), suffix=path.suffix)

    raw_text = _extract_text(path)
    if not raw_text.strip():
        raise ValueError(f"Resume file appears empty or unreadable: {path}")

    log.info("text_extracted", chars=len(raw_text))

    # LLM structured extraction
    resume = await _llm_extract(raw_text)

    # Supplement: extract any skills regex found that LLM missed
    regex_skills = _regex_skills(raw_text)
    all_skills = list(dict.fromkeys(resume.skills + regex_skills))  # ordered dedup
    resume = resume.model_copy(update={"skills": all_skills, "raw_text": raw_text})

    log.info("resume_parsed",
             name=resume.name, skills=len(resume.skills),
             experiences=len(resume.experiences), education=len(resume.education))
    return resume


# ── Text extraction ───────────────────────────────────────────────────────────

def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _pdf_text(path)
    elif suffix in (".docx", ".doc"):
        return _docx_text(path)
    elif suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="ignore")
    else:
        raise ValueError(f"Unsupported resume format: {suffix}. Use PDF, DOCX, or TXT.")


def _pdf_text(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(path))
        if text.strip():
            return text
    except Exception as e:
        log.warning("pdfminer_failed", error=str(e))

    # Fallback: pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        log.warning("pypdf_failed", error=str(e))
        return ""


def _docx_text(path: Path) -> str:
    try:
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        log.warning("docx_extract_failed", error=str(e))
        return ""


# ── LLM extraction ────────────────────────────────────────────────────────────

async def _llm_extract(raw_text: str) -> MasterResume:
    """Call LLM to extract structured data. Fall back to regex on failure."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    # Truncate to 6000 chars (fits within context, covers most resumes)
    truncated = raw_text[:6000]

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=PARSE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Parse this resume:\n\n{truncated}"}],
        )
        raw_json = response.content[0].text
        raw_json = re.sub(r"```(?:json)?|```", "", raw_json).strip()
        data = json.loads(raw_json)
        return _build_master_resume(data, raw_text)

    except json.JSONDecodeError as e:
        log.warning("llm_json_parse_failed", error=str(e))
        return _regex_fallback(raw_text)

    except Exception as e:
        log.error("llm_extraction_failed", error=str(e))
        return _regex_fallback(raw_text)


def _build_master_resume(data: dict, raw_text: str) -> MasterResume:
    """Build MasterResume from LLM-parsed dict. Handles missing/malformed fields."""

    experiences = []
    for exp in data.get("experiences", []):
        try:
            experiences.append(ResumeExperience(**{
                "title":           exp.get("title", ""),
                "company":         exp.get("company", ""),
                "start":           str(exp.get("start", "")),
                "end":             str(exp.get("end", "")) if exp.get("end") else None,
                "bullets":         exp.get("bullets", []),
                "skills_mentioned": exp.get("skills_mentioned", []),
            }))
        except Exception as e:
            log.debug("exp_parse_error", error=str(e))

    education = []
    for edu in data.get("education", []):
        try:
            education.append(ResumeEducation(**{
                "degree":      edu.get("degree", ""),
                "field":       edu.get("field", ""),
                "institution": edu.get("institution", ""),
                "gpa":         float(edu["gpa"]) if edu.get("gpa") else None,
                "year":        int(edu.get("year", 2025)),
            }))
        except Exception as e:
            log.debug("edu_parse_error", error=str(e))

    return MasterResume(
        raw_text=raw_text,
        name=data.get("name", "Unknown"),
        email=data.get("email", ""),
        phone=data.get("phone"),
        linkedin=data.get("linkedin"),
        github=data.get("github"),
        portfolio=data.get("portfolio"),
        summary=data.get("summary"),
        skills=[s.lower().strip() for s in data.get("skills", []) if s],
        experiences=experiences,
        education=education,
        publications=data.get("publications", []),
        certifications=data.get("certifications", []),
        languages=data.get("languages", []),
    )


# ── Regex fallback ────────────────────────────────────────────────────────────

TECH_SKILLS_PATTERN = re.compile(
    r"\b(python|java|c\+\+|javascript|typescript|rust|go|matlab|r\b|julia|"
    r"pytorch|tensorflow|keras|sklearn|scikit.learn|numpy|pandas|scipy|"
    r"machine learning|deep learning|nlp|computer vision|reinforcement learning|"
    r"llm|transformer|bert|gpt|diffusion|"
    r"docker|kubernetes|git|linux|aws|gcp|azure|"
    r"sql|postgresql|mysql|mongodb|redis|"
    r"react|node|fastapi|flask|django|"
    r"latex|matlab|mathematica|"
    r"bioinformatics|genomics|proteomics|cryo.em|molecular dynamics)\b",
    re.IGNORECASE,
)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _regex_skills(text: str) -> list[str]:
    found = set(m.group(0).lower() for m in TECH_SKILLS_PATTERN.finditer(text))
    return sorted(found)


def _regex_fallback(raw_text: str) -> MasterResume:
    """Minimal regex extraction when LLM fails completely."""
    log.warning("using_regex_fallback_parser")
    email_match = EMAIL_PATTERN.search(raw_text)
    return MasterResume(
        raw_text=raw_text,
        name="Unknown (parse failed)",
        email=email_match.group(0) if email_match else "",
        skills=_regex_skills(raw_text),
        experiences=[],
        education=[],
    )
