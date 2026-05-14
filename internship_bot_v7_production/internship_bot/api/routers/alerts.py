"""
Alert Webhook Router — receives POST from Alertmanager, fires notifications.

Alertmanager → POST /internal/alert-webhook → TelegramNotifier / EmailNotifier

Security: bearer token check (INTERNAL_WEBHOOK_TOKEN env var).
Only reachable from inside Docker network — not exposed via Nginx.
"""

from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/internal", tags=["internal"])

WEBHOOK_TOKEN = os.environ.get("INTERNAL_WEBHOOK_TOKEN", "change-me")


# ── Alertmanager payload schema ───────────────────────────────────────────────

class AlertAnnotations(BaseModel):
    summary: str = ""
    description: str = ""


class Alert(BaseModel):
    status: str                    # "firing" | "resolved"
    labels: dict[str, str] = {}
    annotations: AlertAnnotations = AlertAnnotations()
    startsAt: str = ""
    endsAt: str = ""


class AlertmanagerPayload(BaseModel):
    receiver: str = ""
    status: str = ""
    alerts: list[Alert] = []
    groupLabels: dict[str, str] = {}
    commonLabels: dict[str, str] = {}
    commonAnnotations: dict[str, str] = {}


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@router.post("/alert-webhook")
async def alert_webhook(
    payload: AlertmanagerPayload,
    authorization: str = Header(default=""),
):
    """Receive Alertmanager webhook → fire Telegram + email notifications."""

    # Bearer token check
    expected = f"Bearer {WEBHOOK_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from notifications.notifier import NotificationManager, NotificationEvent
    notifier = NotificationManager.from_settings()

    for alert in payload.alerts:
        if alert.status != "firing":
            continue   # skip resolved — noisy

        name   = alert.labels.get("alertname", "UnknownAlert")
        sev    = alert.labels.get("severity", "info")
        summary = alert.annotations.summary
        desc    = alert.annotations.description

        # Route by alert name
        if name == "InterviewInviteReceived":
            await notifier.notify(NotificationEvent.INTERVIEW, {
                "company": "—",
                "role": "—",
                "country": "—",
            })

        elif name == "OfferReceived":
            await notifier.notify(NotificationEvent.OFFER, {
                "company": "—",
                "role": "—",
            })

        elif name == "HumanQueueBacklog":
            # Fetch current queue from DB and send count
            await notifier.notify(NotificationEvent.HUMAN_QUEUE, {
                "items": [],
            })

        else:
            # Generic error/warning → send as pipeline error
            await notifier.notify(NotificationEvent.PIPELINE_ERR, {
                "agent": name,
                "message": f"{summary}\n{desc}",
                "severity": sev,
            })

    return {"received": len(payload.alerts), "ts": datetime.now(timezone.utc).isoformat()}
