"""
Notification System — email (SMTP) + Telegram alerts.

Events that trigger notifications:
  INTERVIEW_INVITE  → urgent Telegram + email
  OFFER             → urgent Telegram + email
  REJECTION         → email digest (batched, not per-rejection)
  HUMAN_QUEUE       → Telegram alert (needs your action)
  PIPELINE_ERROR    → Telegram (CRITICAL only) or email (WARNING)
  DAILY_SUMMARY     → email every morning with stats

Config (add to .env):
  NOTIFY_EMAIL_TO=you@gmail.com
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=bot@gmail.com
  SMTP_PASS=app-specific-password
  TELEGRAM_BOT_TOKEN=123456:ABC...
  TELEGRAM_CHAT_ID=-100123456789
"""

from __future__ import annotations
import asyncio
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


# ── Event types ───────────────────────────────────────────────────────────────

class NotificationEvent:
    INTERVIEW    = "interview_invite"
    OFFER        = "offer"
    REJECTION    = "rejection"
    HUMAN_QUEUE  = "human_queue"
    PIPELINE_ERR = "pipeline_error"
    DAILY_SUMMARY= "daily_summary"
    SUBMITTED    = "submitted"


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Notifier
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Send messages to a Telegram chat/group via Bot API.
    Uses MarkdownV2 formatting.

    Setup:
      1. Create bot via @BotFather → get TELEGRAM_BOT_TOKEN
      2. Add bot to group/channel → get TELEGRAM_CHAT_ID
      3. Set env vars

    Rate limits: Telegram allows 30 messages/sec. We never hit that.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._url = self.API_URL.format(token=token)

    async def send(self, text: str, urgent: bool = False) -> bool:
        """Send markdown message. Returns True on success."""
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(self._url, json=payload)
                if r.status_code == 200:
                    return True
                log.warning("telegram_send_failed", status=r.status_code, body=r.text[:200])
                return False
        except Exception as e:
            log.error("telegram_error", error=str(e))
            return False

    async def send_interview_alert(self, company: str, role: str, country: str) -> None:
        msg = (
            f"🎉 *INTERVIEW INVITE!*\n\n"
            f"*Company:* {self._esc(company)}\n"
            f"*Role:* {self._esc(role)}\n"
            f"*Country:* {country.upper()}\n\n"
            f"Check your email and reply within 24h\\!"
        )
        await self.send(msg, urgent=True)

    async def send_offer_alert(self, company: str, role: str) -> None:
        msg = (
            f"🏆 *OFFER RECEIVED!*\n\n"
            f"*Company:* {self._esc(company)}\n"
            f"*Role:* {self._esc(role)}\n\n"
            f"Congratulations\\! Check email for details\\."
        )
        await self.send(msg, urgent=True)

    async def send_human_queue_alert(self, count: int, items: list[dict]) -> None:
        preview = "\n".join(
            f"• {i.get('company','?')} — {i.get('title','?')[:40]}"
            for i in items[:5]
        )
        msg = (
            f"⚠️ *{count} application(s) need your review*\n\n"
            f"{preview}\n\n"
            f"Open dashboard → Human Queue to approve or skip\\."
        )
        await self.send(msg)

    async def send_error_alert(self, agent: str, message: str, severity: str) -> None:
        emoji = "🔴" if severity == "critical" else "🟡"
        msg = (
            f"{emoji} *Pipeline {severity.upper()}*\n\n"
            f"*Agent:* {self._esc(agent)}\n"
            f"*Error:* {self._esc(message[:200])}"
        )
        await self.send(msg, urgent=(severity == "critical"))

    async def send_daily_summary(self, stats: dict) -> None:
        msg = (
            f"📊 *Daily Summary — {datetime.now(timezone.utc).strftime('%b %d')}*\n\n"
            f"Total apps: *{stats.get('total',0)}*\n"
            f"Submitted today: *{stats.get('submitted_today',0)}*\n"
            f"Responses: *{stats.get('responses',0)}*\n"
            f"Interviews: *{stats.get('interviews',0)}*\n"
            f"Queue: *{stats.get('human_queue',0)}* pending review"
        )
        await self.send(msg)

    @staticmethod
    def _esc(text: str) -> str:
        """Escape Markdown special chars."""
        for ch in r"\_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text


# ═══════════════════════════════════════════════════════════════════════════════
# Email Notifier
# ═══════════════════════════════════════════════════════════════════════════════

class EmailNotifier:
    """SMTP email sender. Gmail app password recommended."""

    def __init__(self, host: str, port: int, user: str, password: str, to: str):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._to = to

    async def send(self, subject: str, html_body: str) -> bool:
        """Send HTML email. Runs SMTP in thread (blocking)."""
        try:
            await asyncio.to_thread(self._send_sync, subject, html_body)
            return True
        except Exception as e:
            log.error("email_send_failed", error=str(e))
            return False

    def _send_sync(self, subject: str, html_body: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Internship Bot <{self._user}>"
        msg["To"] = self._to
        msg.attach(MIMEText(html_body, "html"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP(self._host, self._port) as server:
            server.starttls(context=ctx)
            server.login(self._user, self._password)
            server.sendmail(self._user, self._to, msg.as_string())

    async def send_interview_alert(self, company: str, role: str, country: str, url: str = "") -> None:
        await self.send(
            subject=f"🎉 Interview Invite — {company}",
            html_body=_email_template(
                title="Interview Invite!",
                color="#22c55e",
                emoji="🎉",
                body=f"""
                <p>You received an interview invite for:</p>
                <table style="margin:16px 0">
                  <tr><td style="color:#64748b;padding-right:16px">Company</td><td><strong>{company}</strong></td></tr>
                  <tr><td style="color:#64748b;padding-right:16px">Role</td><td>{role}</td></tr>
                  <tr><td style="color:#64748b;padding-right:16px">Country</td><td>{country.upper()}</td></tr>
                </table>
                <p>Check your inbox and reply within <strong>24 hours</strong>.</p>
                {f'<p><a href="{url}">View posting ↗</a></p>' if url else ''}
                """,
            ),
        )

    async def send_offer_alert(self, company: str, role: str) -> None:
        await self.send(
            subject=f"🏆 Offer Received — {company}",
            html_body=_email_template(
                title="Offer Received!",
                color="#6366f1",
                emoji="🏆",
                body=f"<p>Congratulations! You received an offer from <strong>{company}</strong> for <strong>{role}</strong>. Check your email for details.</p>",
            ),
        )

    async def send_rejection_digest(self, rejections: list[dict]) -> None:
        rows = "".join(
            f"<tr><td>{r.get('company')}</td><td>{r.get('title')}</td><td>{r.get('country','').upper()}</td></tr>"
            for r in rejections
        )
        await self.send(
            subject=f"Application Update — {len(rejections)} rejection(s)",
            html_body=_email_template(
                title=f"{len(rejections)} Rejection(s)",
                color="#ef4444",
                emoji="📋",
                body=f"""
                <table style="width:100%;border-collapse:collapse;margin-top:12px">
                  <tr style="background:#f8fafc"><th style="text-align:left;padding:8px">Company</th><th style="text-align:left;padding:8px">Role</th><th style="text-align:left;padding:8px">Country</th></tr>
                  {rows}
                </table>
                <p style="margin-top:16px;color:#64748b;font-size:13px">Keep going — the analytics system is adjusting your targets based on this feedback.</p>
                """,
            ),
        )

    async def send_daily_summary(self, stats: dict) -> None:
        await self.send(
            subject=f"📊 Daily Summary — {datetime.now(timezone.utc).strftime('%b %d, %Y')}",
            html_body=_email_template(
                title="Daily Summary",
                color="#3b82f6",
                emoji="📊",
                body=f"""
                <table style="width:100%;border-collapse:collapse">
                  <tr><td style="padding:8px;color:#64748b">Total Applications</td><td style="padding:8px;font-weight:bold">{stats.get('total',0)}</td></tr>
                  <tr style="background:#f8fafc"><td style="padding:8px;color:#64748b">Submitted Today</td><td style="padding:8px;font-weight:bold">{stats.get('submitted_today',0)}</td></tr>
                  <tr><td style="padding:8px;color:#64748b">Total Responses</td><td style="padding:8px;font-weight:bold">{stats.get('responses',0)}</td></tr>
                  <tr style="background:#f8fafc"><td style="padding:8px;color:#64748b">Interview Invites</td><td style="padding:8px;font-weight:bold;color:#22c55e">{stats.get('interviews',0)}</td></tr>
                  <tr><td style="padding:8px;color:#64748b">Human Queue</td><td style="padding:8px;font-weight:bold;color:#f59e0b">{stats.get('human_queue',0)}</td></tr>
                  <tr style="background:#f8fafc"><td style="padding:8px;color:#64748b">Response Rate</td><td style="padding:8px;font-weight:bold">{stats.get('response_rate','—')}</td></tr>
                </table>
                """,
            ),
        )


def _email_template(title: str, color: str, emoji: str, body: str) -> str:
    return f"""
    <html><body style="font-family:Arial,sans-serif;background:#f8fafc;padding:24px;color:#1e293b">
    <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">
      <div style="background:{color};padding:20px 24px;color:#fff">
        <h1 style="margin:0;font-size:20px">{emoji} {title}</h1>
        <p style="margin:4px 0 0;opacity:.8;font-size:13px">{datetime.now(timezone.utc).strftime('%B %d, %Y')}</p>
      </div>
      <div style="padding:24px">{body}</div>
      <div style="padding:16px 24px;background:#f8fafc;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8">
        Internship Auto-Apply Bot · <a href="http://localhost:8000" style="color:#6366f1">Open Dashboard</a>
      </div>
    </div>
    </body></html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# Notification Manager — single interface for the pipeline to use
# ═══════════════════════════════════════════════════════════════════════════════

class NotificationManager:
    """
    Unified notification interface. Pipeline calls this — doesn't care
    whether it's Telegram, email, or both.
    """

    def __init__(self):
        self._telegram: TelegramNotifier | None = None
        self._email: EmailNotifier | None = None
        self._pending_rejections: list[dict] = []

    @classmethod
    def from_settings(cls) -> "NotificationManager":
        from config.settings import settings
        mgr = cls()

        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
            mgr._telegram = TelegramNotifier(
                settings.TELEGRAM_BOT_TOKEN,
                settings.TELEGRAM_CHAT_ID,
            )
            log.info("telegram_notifier_enabled")
        else:
            log.warning("telegram_not_configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")

        if settings.SMTP_USER and settings.NOTIFY_EMAIL_TO:
            mgr._email = EmailNotifier(
                host=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                user=settings.SMTP_USER,
                password=settings.SMTP_PASS,
                to=settings.NOTIFY_EMAIL_TO,
            )
            log.info("email_notifier_enabled", to=settings.NOTIFY_EMAIL_TO)
        else:
            log.warning("email_not_configured — set SMTP_USER + NOTIFY_EMAIL_TO")

        return mgr

    async def notify(self, event: str, payload: dict) -> None:
        """Route event to correct notification handlers."""
        company = payload.get("company", "?")
        role    = payload.get("role", "?")
        country = payload.get("country", "?")

        if event == NotificationEvent.INTERVIEW:
            tasks = []
            if self._telegram:
                tasks.append(self._telegram.send_interview_alert(company, role, country))
            if self._email:
                tasks.append(self._email.send_interview_alert(company, role, country, payload.get("url","")))
            await asyncio.gather(*tasks, return_exceptions=True)

        elif event == NotificationEvent.OFFER:
            tasks = []
            if self._telegram:
                tasks.append(self._telegram.send_offer_alert(company, role))
            if self._email:
                tasks.append(self._email.send_offer_alert(company, role))
            await asyncio.gather(*tasks, return_exceptions=True)

        elif event == NotificationEvent.REJECTION:
            # Buffer rejections — send as digest, not per-rejection spam
            self._pending_rejections.append(payload)
            if len(self._pending_rejections) >= 5:
                await self._flush_rejections()

        elif event == NotificationEvent.HUMAN_QUEUE:
            if self._telegram:
                items = payload.get("items", [])
                await self._telegram.send_human_queue_alert(len(items), items)

        elif event == NotificationEvent.PIPELINE_ERR:
            if self._telegram:
                await self._telegram.send_error_alert(
                    payload.get("agent", "?"),
                    payload.get("message", "?"),
                    payload.get("severity", "warning"),
                )

        elif event == NotificationEvent.DAILY_SUMMARY:
            tasks = []
            if self._telegram:
                tasks.append(self._telegram.send_daily_summary(payload))
            if self._email:
                tasks.append(self._email.send_daily_summary(payload))
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _flush_rejections(self) -> None:
        if not self._pending_rejections:
            return
        if self._email:
            await self._email.send_rejection_digest(self._pending_rejections)
        self._pending_rejections.clear()

    async def flush(self) -> None:
        """Call on shutdown to send any buffered notifications."""
        await self._flush_rejections()
