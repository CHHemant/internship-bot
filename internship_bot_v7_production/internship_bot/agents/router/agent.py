"""
Application Router Agent.

Receives: verified resume + cover letter + job listing + user prefs
Produces: SubmissionPackage — everything the Form Filler needs to operate.

Responsibilities:
  1. Detect portal type from URL
  2. Validate user has required documents for that portal
  3. Assemble field mapping (name, email, work-auth → form fields)
  4. Handle extra doc requirements per portal (DAAD funding statement, Euraxess CV)
  5. Flag unrecognized portals for human review
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from agents.base_agent import BaseAgent
from agents.router.portal_configs import PortalConfig, PortalType, detect_portal
from models.schemas import (
    Country,
    ErrorSeverity,
    JobListing,
    MasterResume,
    UserPrefs,
)


@dataclass
class SubmissionPackage:
    """Everything the Form Filler needs — no further lookups required."""
    listing: JobListing
    portal: PortalConfig
    resume_path: Path
    cover_letter_path: Path
    field_map: dict[str, str]            # logical field name → candidate value
    extra_docs: dict[str, Path]          # doc type → file path (empty if not needed)
    session_notes: list[str]             # hints for form filler (e.g. "use Easy Apply")
    requires_human_review: bool = False  # escalate before submit if True


# ─── Field values we know from candidate data ─────────────────────────────────

def _build_field_map(master: MasterResume, listing: JobListing, prefs: UserPrefs) -> dict[str, str]:
    """
    Standard field values any portal might ask for.
    Form filler maps these to actual DOM fields via portal-specific selectors.
    """
    work_auth = prefs.work_auth.get(listing.country, False)
    return {
        "first_name":          _first(master.name),
        "last_name":           _last(master.name),
        "full_name":           master.name,
        "email":               master.email,
        "phone":               master.phone or "",
        "linkedin":            master.linkedin or "",
        "github":              master.github or "",
        "portfolio":           master.portfolio or "",
        "work_authorization":  "yes" if work_auth else "no",
        "years_experience":    str(_total_experience_years(master)),
        "highest_degree":      master.education[0].degree if master.education else "",
        "institution":         master.education[0].institution if master.education else "",
        "graduation_year":     str(master.education[0].year) if master.education else "",
        "gpa":                 str(master.education[0].gpa) if master.education and master.education[0].gpa else "",
        "role_title":          listing.title,
        "company":             listing.company,
        "country":             listing.country.value.upper(),
    }


class ApplicationRouterAgent(BaseAgent):

    async def run(
        self,
        listing: JobListing,
        resume_text: str,
        cover_text: str,
        prefs: UserPrefs,
        master: MasterResume | None = None,
        extra_docs_paths: dict[str, Path] | None = None,
    ) -> SubmissionPackage:

        portal = detect_portal(str(listing.url))
        self.log.info("portal_detected", portal=portal.type, listing=listing.id)

        # Write resume and cover letter to temp files
        resume_path = self._write_temp(resume_text, f"resume_{listing.id}.txt")
        cover_path  = self._write_temp(cover_text,  f"cover_{listing.id}.txt")

        field_map = _build_field_map(master, listing, prefs) if master else {}
        extra_docs: dict[str, Path] = {}
        session_notes: list[str] = []
        requires_human = False

        # ── Portal-specific routing logic ─────────────────────────────────────

        if portal.type == PortalType.LINKEDIN:
            session_notes.append("Attempt Easy Apply first. If button absent → full apply form.")
            session_notes.append("Check for 'Work Authorization' dropdown — set from field_map.")

        elif portal.type == PortalType.DAAD:
            session_notes.append("Login to DAAD Bewerbungsportal with stored credentials.")
            session_notes.append("Upload: CV (PDF) + funding statement + motivation letter.")
            session_notes.append("Date fields use DD.MM.YYYY — reformat all dates.")
            # Check for required extra docs
            funding_stmt = (extra_docs_paths or {}).get("funding_statement")
            if funding_stmt and funding_stmt.exists():
                extra_docs["funding_statement"] = funding_stmt
            else:
                self.log.warning("daad_missing_funding_statement", listing=listing.id)
                session_notes.append("⚠ FUNDING STATEMENT MISSING — generate before submitting.")
                requires_human = True

        elif portal.type == PortalType.EURAXESS:
            session_notes.append("Upload Europass CV format PDF — standard resume rejected by some postings.")
            session_notes.append("Select research field code from taxonomy dropdown — match JD keywords.")
            session_notes.append("Two recommendation letter uploads may be required — flag if missing.")
            europass = (extra_docs_paths or {}).get("europass_cv")
            if europass and europass.exists():
                extra_docs["europass_cv"] = europass
            else:
                session_notes.append("⚠ EUROPASS CV MISSING — standard CV used as fallback.")

        elif portal.type == PortalType.HANDSHAKE:
            session_notes.append("Login with university email. Use Easy Apply if available.")
            session_notes.append("GPA field: use 4.0 scale unless EU — convert if needed.")
            session_notes.append("Work auth dropdown: select F-1 OPT / CPT / None per prefs.")

        elif portal.type == PortalType.UNIVERSITY:
            session_notes.append("University portal — generic selectors. Verify fields before submit.")
            session_notes.append("Cover letter text box common: paste content directly if upload not available.")
            requires_human = True   # university portals vary too much — always flag

        elif portal.type == PortalType.GENERIC:
            session_notes.append("Unknown portal type. Generic selectors only. Human review required.")
            requires_human = True
            await self._emit_error(
                application_id=listing.id,
                severity=ErrorSeverity.STRUCTURAL,
                message=f"Unknown portal for URL: {listing.url}",
                context={"portal_type": portal.type},
            )

        return SubmissionPackage(
            listing=listing,
            portal=portal,
            resume_path=resume_path,
            cover_letter_path=cover_path,
            field_map=field_map,
            extra_docs=extra_docs,
            session_notes=session_notes,
            requires_human_review=requires_human,
        )

    @staticmethod
    def _write_temp(content: str, filename: str) -> Path:
        import tempfile
        path = Path(tempfile.gettempdir()) / filename
        path.write_text(content, encoding="utf-8")
        return path


# ─── Utilities ────────────────────────────────────────────────────────────────

def _first(name: str) -> str:
    return name.split()[0] if name else ""

def _last(name: str) -> str:
    parts = name.split()
    return " ".join(parts[1:]) if len(parts) > 1 else ""

def _total_experience_years(master: MasterResume) -> int:
    """Rough estimate from experience list."""
    from datetime import datetime
    total = 0
    for exp in master.experiences:
        try:
            start = int(exp.start[-4:])
            end = int(exp.end[-4:]) if exp.end else datetime.now().year
            total += max(0, end - start)
        except (ValueError, TypeError, IndexError):
            pass
    return min(total, 20)  # cap sanity
