"""
BaseAgent v2 — all LLM calls, errors auto-tracked in Prometheus.
"""

from __future__ import annotations
import time
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, TypeVar

import structlog
from anthropic import AsyncAnthropic
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models.schemas import PipelineError, ErrorSeverity

T = TypeVar("T")
log = structlog.get_logger()


def with_retry(max_attempts: int = 3, min_wait: float = 2.0, max_wait: float = 30.0):
    def decorator(fn):
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception_type((ConnectionError, TimeoutError)),
            reraise=True,
        )
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            return await fn(*args, **kwargs)
        return wrapper
    return decorator


class BaseAgent(ABC):

    MODEL = "claude-sonnet-4-20250514"
    MAX_TOKENS = 4096

    def __init__(self, error_bus=None):
        self.llm = AsyncAnthropic()
        self.log = structlog.get_logger(agent=self.__class__.__name__)
        self._error_bus = error_bus
        self._agent_name = self.__class__.__name__

    async def _llm(self, system: str, user: str, max_tokens: int | None = None) -> str:
        """LLM call — automatically tracked in Prometheus."""
        tokens = max_tokens or self.MAX_TOKENS
        t0 = time.monotonic()

        try:
            from infra.metrics import llm_calls_total, llm_call_duration, llm_tokens_used
            llm_calls_total.labels(agent=self._agent_name).inc()
            _track = True
        except ImportError:
            _track = False

        try:
            response = await self.llm.messages.create(
                model=self.MODEL,
                max_tokens=tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = response.content[0].text
            if _track:
                llm_call_duration.labels(agent=self._agent_name).observe(time.monotonic() - t0)
                llm_tokens_used.labels(agent=self._agent_name).inc(
                    (len(system) + len(user) + len(text)) // 4
                )
            return text
        except Exception:
            if _track:
                llm_call_duration.labels(agent=self._agent_name).observe(time.monotonic() - t0)
            raise

    async def _emit_error(
        self,
        application_id: str,
        severity: ErrorSeverity,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        err = PipelineError(
            application_id=application_id,
            agent=self._agent_name,
            severity=severity,
            message=message,
            context=context or {},
        )
        self.log.error("agent_error", **err.model_dump())
        try:
            from infra.metrics import pipeline_errors
            pipeline_errors.labels(severity=severity.value, agent=self._agent_name).inc()
        except ImportError:
            pass
        if self._error_bus:
            await self._error_bus.publish(err)

    @abstractmethod
    async def run(self, *args, **kwargs) -> Any: ...
