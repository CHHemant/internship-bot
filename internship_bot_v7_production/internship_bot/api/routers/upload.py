"""
Upload router — handles resume file upload and prefs editing via the web UI.

Accepts PDF, DOCX, or TXT. Parses it immediately and stores the result in
the session (in-memory for now, Redis-backed TODO).

TODO: add proper session management with JWT — right now it's one global
       state which is fine for single-user but breaks with multiple users
TODO: add file size validation server-side (client does it too but never trust client)
FIXME: parsed resume is not persisted across API restarts — add Redis cache
"""

import io
import json
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/upload", tags=["upload"])

# in-memory store — good enough for single user
# TODO: replace with Redis when adding multi-user
_session: dict = {
    "master_resume": None,   # parsed MasterResume dict
    "prefs": None,            # UserPrefs dict
}

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB


@router.post("/resume")
async def upload_resume(file: UploadFile = File(...)):
    """
    Upload a resume file (PDF/DOCX/TXT).
    Parses it and returns the extracted data so the UI can show a preview.
    """
    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File too large (max 5MB, got {len(content) // 1024}KB)")

    suffix = Path(file.filename or "resume.pdf").suffix.lower()
    if suffix not in (".pdf", ".docx", ".doc", ".txt", ".md"):
        raise HTTPException(400, f"Unsupported format: {suffix}. Use PDF, DOCX, or TXT.")

    # write to temp file so our parser can read it
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        from agents.job_discovery.resume_parser import parse_resume
        resume = await parse_resume(tmp_path)
        _session["master_resume"] = resume.model_dump()
        return {
            "ok": True,
            "name": resume.name,
            "email": resume.email,
            "skills_count": len(resume.skills),
            "skills_preview": resume.skills[:10],
            "experience_count": len(resume.experiences),
            "education_count": len(resume.education),
            "publications_count": len(resume.publications),
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to parse resume: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/resume")
async def get_resume():
    """Return currently loaded resume summary."""
    if not _session["master_resume"]:
        raise HTTPException(404, "No resume uploaded yet")
    r = _session["master_resume"]
    return {
        "name": r.get("name"),
        "email": r.get("email"),
        "skills": r.get("skills", []),
        "experiences": [
            {"title": e["title"], "company": e["company"]}
            for e in r.get("experiences", [])
        ],
        "education": r.get("education", []),
        "publications": r.get("publications", []),
    }


@router.post("/prefs")
async def save_prefs(prefs: dict):
    """Save user preferences (target countries, domains, work auth, etc.)"""
    from models.schemas import UserPrefs
    try:
        validated = UserPrefs(**prefs)
        _session["prefs"] = validated.model_dump()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, f"Invalid prefs: {e}")


@router.get("/prefs")
async def get_prefs():
    """Return currently saved prefs, or sensible defaults."""
    if _session["prefs"]:
        return _session["prefs"]
    # return defaults — user can edit from here
    return {
        "target_countries": ["usa", "germany", "canada"],
        "target_domains": ["ML research", "NLP"],
        "min_deadline_days": 7,
        "work_auth": {},
        "max_concurrent_apps": 3,
        "dry_run": True,
    }


@router.post("/run")
async def run_with_uploaded():
    """Start a pipeline run using the currently uploaded resume + saved prefs."""
    if not _session["master_resume"]:
        raise HTTPException(400, "Upload a resume first")
    if not _session["prefs"]:
        raise HTTPException(400, "Save preferences first")

    from infra.celery_app import PipelineDispatcher
    from agents.job_discovery.platforms import PLATFORMS

    prefs = _session["prefs"]
    platform_ids = [p.id for p in PLATFORMS[:6]]
    queries = [f"{d} research internship" for d in prefs.get("target_domains", ["research"])[:2]]

    task_ids = PipelineDispatcher.dispatch_discovery(
        platform_ids=platform_ids,
        queries=queries,
        countries=prefs.get("target_countries", ["usa"]),
        master_resume_json=_session["master_resume"],
        prefs_json=prefs,
    )
    return {"dispatched": len(task_ids), "task_ids": task_ids[:5]}
