"""
Form Filler Agent — dispatcher and orchestrator for all portal fillers.

Receives: SubmissionPackage from Router
Selects:  correct BasePortalFiller subclass
Runs:     fill_form() inside managed browser session
Returns:  filled_state dict (screenshot evidence + URL + status)

CAPTCHA → immediate escalation, no bypass
Network timeout → retry 3× with fresh browser session
Human-flagged portals → skips automation, returns PENDING_HUMAN state
"""

from __future__ import annotations
from typing import Any

import structlog

from agents.base_agent import BaseAgent
from agents.form_filler.base_filler import (
    BasePortalFiller,
    CaptchaDetected,
    FormFillError,
)
from agents.form_filler.portals.linkedin import LinkedInFiller
from agents.form_filler.portals.other_portals import (
    DAADFiller,
    EuraxessFiller,
    HandshakeFiller,
    UniversityFiller,
)
from agents.router.portal_configs import PortalType
from agents.router.agent import SubmissionPackage
from models.schemas import ErrorSeverity

log = structlog.get_logger()

MAX_BROWSER_RETRIES = 3

# ── Portal type → filler class ───────────────────────────────────────────────
FILLER_MAP: dict[PortalType, type[BasePortalFiller]] = {
    PortalType.LINKEDIN:  LinkedInFiller,
    PortalType.DAAD:      DAADFiller,
    PortalType.EURAXESS:  EuraxessFiller,
    PortalType.HANDSHAKE: HandshakeFiller,
    PortalType.UNIVERSITY: UniversityFiller,
    PortalType.GENERIC:   UniversityFiller,  # best-effort generic
}


class FormFillerAgent(BaseAgent):

    async def run(self, package: SubmissionPackage) -> dict[str, Any]:
        """
        Returns filled_state dict:
          {
            "status": "ready_to_submit" | "pending_human" | "error",
            "url": str,
            "screenshots": [...],
            "portal": PortalType,
            "notes": [...]
          }
        """
        listing_id = package.listing.id

        # Human-flagged portals skip automation
        if package.requires_human_review:
            self.log.warning("human_review_required", listing=listing_id, portal=package.portal.type)
            return {
                "status": "pending_human",
                "url": str(package.listing.url),
                "screenshots": [],
                "portal": package.portal.type,
                "notes": package.session_notes,
            }

        filler_cls = FILLER_MAP.get(package.portal.type, UniversityFiller)
        filler = filler_cls(headless=True)

        last_error = None
        for attempt in range(1, MAX_BROWSER_RETRIES + 1):
            try:
                self.log.info("form_fill_attempt",
                              listing=listing_id, portal=package.portal.type, attempt=attempt)
                state = await filler.fill_and_return(package)
                state["notes"] = package.session_notes
                self.log.info("form_fill_success", listing=listing_id, attempt=attempt)
                return state

            except CaptchaDetected as e:
                # CAPTCHA → no retry, immediate human escalation
                self.log.error("captcha_escalation", listing=listing_id)
                await self._emit_error(
                    application_id=listing_id,
                    severity=ErrorSeverity.STRUCTURAL,
                    message=f"CAPTCHA detected on {package.portal.type}: {e}",
                )
                return {
                    "status": "pending_human",
                    "url": str(package.listing.url),
                    "screenshots": filler._screenshots,
                    "portal": package.portal.type,
                    "notes": ["CAPTCHA encountered — requires human to complete"],
                }

            except FormFillError as e:
                # Form structure changed or required field missing
                self.log.error("form_fill_error", listing=listing_id, error=str(e), attempt=attempt)
                last_error = str(e)
                if attempt == MAX_BROWSER_RETRIES:
                    await self._emit_error(
                        application_id=listing_id,
                        severity=ErrorSeverity.STRUCTURAL,
                        message=f"FormFillError after {attempt} attempts: {e}",
                    )
                    return {
                        "status": "error",
                        "url": str(package.listing.url),
                        "screenshots": filler._screenshots,
                        "portal": package.portal.type,
                        "notes": [f"FormFillError: {e}"],
                    }

            except (ConnectionError, TimeoutError) as e:
                # Transient network error — retry
                self.log.warning("transient_error", listing=listing_id, error=str(e), attempt=attempt)
                last_error = str(e)
                if attempt == MAX_BROWSER_RETRIES:
                    await self._emit_error(
                        application_id=listing_id,
                        severity=ErrorSeverity.TRANSIENT,
                        message=f"Network error after {attempt} retries: {e}",
                    )
                    break

        return {
            "status": "error",
            "url": str(package.listing.url),
            "screenshots": filler._screenshots,
            "portal": package.portal.type,
            "notes": [f"Failed after {MAX_BROWSER_RETRIES} attempts: {last_error}"],
        }
