"""
FastAPI metrics middleware + /metrics endpoint.

Adds to api/main.py:
  - Request duration histogram per route + method + status
  - Active requests gauge
  - /metrics endpoint (Prometheus scrape target)
  - Background task: refresh status gauge every 60s
"""

from __future__ import annotations
import asyncio
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    Counter, Gauge, Histogram,
    generate_latest, CONTENT_TYPE_LATEST,
)

# ── HTTP metrics ──────────────────────────────────────────────────────────────

http_requests_total = Counter(
    "bot_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

http_request_duration = Histogram(
    "bot_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "endpoint"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

http_active_requests = Gauge(
    "bot_http_active_requests",
    "Currently active HTTP requests",
)

websocket_connections = Gauge(
    "bot_websocket_connections",
    "Active WebSocket connections",
)


def add_metrics_middleware(app: FastAPI) -> None:
    """
    Call this in api/main.py after creating the FastAPI app.
    Adds middleware + /metrics endpoint.
    """

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        # Exclude /metrics itself from tracking (avoid cardinality explosion)
        if request.url.path == "/metrics":
            return await call_next(request)

        # Normalise path (remove IDs to avoid high cardinality)
        endpoint = _normalise_path(request.url.path)
        method = request.method

        http_active_requests.inc()
        start = time.monotonic()

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            duration = time.monotonic() - start
            http_active_requests.dec()
            http_requests_total.labels(
                method=method, endpoint=endpoint, status_code=str(status)
            ).inc()
            http_request_duration.labels(
                method=method, endpoint=endpoint
            ).observe(duration)

        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint():
        """Prometheus scrape endpoint."""
        data = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    # startup hook registered via lifespan in main.py — see _refresh_status_gauges()
    # kept here as standalone callable
async def start_status_gauge_refresh_standalone():
        """Refresh application status gauges every 60s in background."""
        asyncio.create_task(_refresh_status_gauges())


async def _refresh_status_gauges() -> None:
    """Background task: query DB for status counts, update Prometheus gauges."""
    from infra.metrics import update_status_gauges
    while True:
        try:
            from infra.db.session import get_db
            from infra.db.models import ApplicationRecordORM
            from sqlalchemy import select, func

            async with get_db() as session:
                result = await session.execute(
                    select(
                        ApplicationRecordORM.status,
                        func.count(ApplicationRecordORM.id).label("count"),
                    ).group_by(ApplicationRecordORM.status)
                )
                counts = {row.status: row.count for row in result}

            update_status_gauges(counts)

        except Exception:
            pass  # DB may not be ready yet — silently skip

        await asyncio.sleep(60)


def _normalise_path(path: str) -> str:
    """
    Replace dynamic segments with placeholders to avoid Prometheus
    label cardinality explosion from per-ID paths.

    /api/applications/MIT-abc123  →  /api/applications/{id}
    /api/applications/MIT-abc123/approve  →  /api/applications/{id}/approve
    """
    import re
    # Replace UUIDs and long alphanumeric IDs
    path = re.sub(r"/[A-Za-z0-9_\-]{8,}", "/{id}", path, count=1)
    return path
