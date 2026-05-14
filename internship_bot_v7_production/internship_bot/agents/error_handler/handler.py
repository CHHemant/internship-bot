"""
Error Handler — centralized error classification and routing.

Every agent publishes PipelineError events here via Redis pub/sub.
Error Handler classifies, retries, or escalates.

5-tier taxonomy:
  TRANSIENT   → auto retry, exponential backoff, max 3×
  STRUCTURAL  → portal layout changed → skip listing, flag for system update
  CONTENT     → ATS never passes → human review queue
  AUTH        → re-auth once → then STRUCTURAL escalation
  CRITICAL    → invalid API key / proxy blocked → halt pipeline, alert

This module has two parts:
  1. ErrorHandler class   — classification + action logic
  2. ErrorBus class       — Redis pub/sub channel all agents write to
"""

from __future__ import annotations
import asyncio
import json
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from models.schemas import ErrorSeverity, PipelineError

log = structlog.get_logger()


# ─── Retry config per severity ────────────────────────────────────────────────

RETRY_CONFIG: dict[ErrorSeverity, dict] = {
    ErrorSeverity.TRANSIENT: {
        "max_retries": 3,
        "backoff_base": 2.0,   # seconds × 2^attempt
        "escalate_after": ErrorSeverity.STRUCTURAL,
    },
    ErrorSeverity.STRUCTURAL: {
        "max_retries": 0,      # no retry — portal structure changed
        "backoff_base": 0,
        "escalate_after": None,  # → human queue
    },
    ErrorSeverity.CONTENT: {
        "max_retries": 0,      # Verifier already retried — go straight to human
        "backoff_base": 0,
        "escalate_after": None,
    },
    ErrorSeverity.AUTH: {
        "max_retries": 1,      # re-auth once
        "backoff_base": 5.0,
        "escalate_after": ErrorSeverity.STRUCTURAL,
    },
    ErrorSeverity.CRITICAL: {
        "max_retries": 0,
        "backoff_base": 0,
        "escalate_after": None,  # halt + alert
    },
}


class ErrorAction(str, Enum):
    RETRY        = "retry"
    SKIP         = "skip"           # skip this listing
    HUMAN_QUEUE  = "human_queue"    # add to human review queue
    HALT         = "halt"           # stop entire pipeline


class ErrorHandler:
    """
    Stateless per-event. Stateful retry tracking via in-memory dict
    (swap for Redis hash in production).
    """

    def __init__(self, notifier=None):
        # application_id → retry count per severity
        self._retry_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._human_queue: list[PipelineError] = []
        self._notifier = notifier   # e.g. send Slack/email alert

    async def handle(self, error: PipelineError) -> ErrorAction:
        """
        Classify error, decide action, execute side-effects.
        Returns the action taken so orchestrator knows what to do next.
        """
        cfg = RETRY_CONFIG[error.severity]
        key = error.application_id
        sev = error.severity.value

        log.error(
            "error_received",
            application=key,
            agent=error.agent,
            severity=sev,
            message=error.message,
        )

        # ── CRITICAL: halt pipeline immediately ───────────────────────────────
        if error.severity == ErrorSeverity.CRITICAL:
            log.critical("pipeline_halt", reason=error.message)
            await self._alert(error, urgent=True)
            return ErrorAction.HALT

        # ── AUTH: re-auth once, then escalate ────────────────────────────────
        if error.severity == ErrorSeverity.AUTH:
            retry_count = self._retry_counts[key][sev]
            if retry_count < cfg["max_retries"]:
                self._retry_counts[key][sev] += 1
                delay = cfg["backoff_base"] * (2 ** retry_count)
                log.info("auth_retry", application=key, delay=delay)
                await asyncio.sleep(delay)
                return ErrorAction.RETRY
            # Auth retry exhausted → escalate to STRUCTURAL
            escalated = PipelineError(
                application_id=key,
                agent=error.agent,
                severity=ErrorSeverity.STRUCTURAL,
                message=f"Auth retry exhausted: {error.message}",
                context=error.context,
            )
            return await self.handle(escalated)

        # ── TRANSIENT: exponential backoff retry ─────────────────────────────
        if error.severity == ErrorSeverity.TRANSIENT:
            retry_count = self._retry_counts[key][sev]
            if retry_count < cfg["max_retries"]:
                delay = cfg["backoff_base"] * (2 ** retry_count)
                self._retry_counts[key][sev] += 1
                log.info("transient_retry", application=key, attempt=retry_count + 1, delay=delay)
                await asyncio.sleep(delay)
                return ErrorAction.RETRY
            # Exhausted → escalate
            log.warning("transient_exhausted", application=key)
            return ErrorAction.SKIP

        # ── STRUCTURAL: skip listing, flag for devs ───────────────────────────
        if error.severity == ErrorSeverity.STRUCTURAL:
            log.warning("structural_skip", application=key, message=error.message)
            await self._alert(error, urgent=False)
            return ErrorAction.SKIP

        # ── CONTENT: ATS fail → human review ─────────────────────────────────
        if error.severity == ErrorSeverity.CONTENT:
            self._human_queue.append(error)
            log.info("added_to_human_queue", application=key, queue_size=len(self._human_queue))
            return ErrorAction.HUMAN_QUEUE

        return ErrorAction.SKIP

    def human_queue_snapshot(self) -> list[dict]:
        """Return current human review queue as serializable list."""
        return [e.model_dump() for e in self._human_queue]

    def clear_retry_state(self, application_id: str) -> None:
        """Call after successful retry to reset counter."""
        self._retry_counts.pop(application_id, None)

    async def _alert(self, error: PipelineError, urgent: bool) -> None:
        """Send Slack/email notification. Pluggable."""
        if self._notifier:
            await self._notifier.send(
                level="CRITICAL" if urgent else "WARNING",
                title=f"Pipeline Error: {error.severity}",
                body=f"Agent: {error.agent}\n{error.message}",
                context=error.context,
            )
        else:
            log.warning("no_notifier_configured", severity=error.severity)


# ─── Redis Error Bus ──────────────────────────────────────────────────────────

class ErrorBus:
    """
    Thin Redis pub/sub wrapper.
    Agents call: await bus.publish(PipelineError(...))
    Error Handler subscribes and processes.

    In testing, pass error_bus=None to all agents — errors are logged only.
    """

    CHANNEL = "internship_bot:errors"

    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._redis = None

    async def connect(self) -> None:
        import redis.asyncio as aioredis
        self._redis = await aioredis.from_url(self._redis_url)

    async def publish(self, error: PipelineError) -> None:
        if self._redis is None:
            log.warning("error_bus_not_connected", error=error.message)
            return
        payload = error.model_dump_json()
        await self._redis.publish(self.CHANNEL, payload)

    async def subscribe_and_handle(self, handler: ErrorHandler) -> None:
        """Run this as a background task — processes all error events."""
        import redis.asyncio as aioredis
        r = await aioredis.from_url(self._redis_url)
        pubsub = r.pubsub()
        await pubsub.subscribe(self.CHANNEL)
        log.info("error_bus_listening", channel=self.CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                error = PipelineError(**data)
                action = await handler.handle(error)
                log.info("error_handled", application=error.application_id, action=action)
            except Exception as e:
                log.error("error_bus_processing_failed", error=str(e))

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
