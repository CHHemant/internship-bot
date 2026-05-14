"""
FastAPI Application — REST API + WebSocket real-time updates + dashboard.

Endpoints:
  GET  /health                      — liveness probe
  GET  /api/applications            — list all apps with filters
  GET  /api/applications/{id}       — single app detail
  POST /api/applications/{id}/approve — approve human-review app for submission
  POST /api/applications/{id}/skip    — skip/discard a pending app
  GET  /api/analytics               — latest analytics snapshot
  GET  /api/queue/human             — human review queue
  GET  /api/platforms               — active platform list + status
  POST /api/run                     — trigger a new discovery + apply cycle
  WS   /ws/updates                  — real-time status push via WebSocket

Dashboard:
  GET  /                            — serves dashboard HTML (embedded)
"""

from __future__ import annotations
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from config.settings import settings
from models.schemas import ApplicationStatus

log = structlog.get_logger()

# ── WebSocket connection manager ───────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._active.remove(ws) if ws in self._active else None

    async def broadcast(self, data: dict) -> None:
        dead = []
        for ws in self._active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()


# ── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from infra.db.session import init_db, create_tables
    init_db(settings.DATABASE_URL)
    await create_tables()
    log.info("api_started", host="0.0.0.0", port=8000)
    yield
    log.info("api_shutdown")


app = FastAPI(
    title="Internship Auto-Apply Bot API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# register routers (were written but never wired in — classic)
from api.routers.auth import router as auth_router
from api.routers.alerts import router as alerts_router
from api.routers.upload import router as upload_router
from api.middleware import add_metrics_middleware
app.include_router(auth_router)
app.include_router(alerts_router)
app.include_router(upload_router)
add_metrics_middleware(app)


# ── Request/Response models ────────────────────────────────────────────────────

class RunRequest(BaseModel):
    resume_path: str
    prefs_override: dict | None = None

class ApplicationSummary(BaseModel):
    id: str
    company: str
    title: str
    country: str
    portal: str
    status: str
    ats_score: float
    submitted_at: str | None
    confirmation_id: str | None

class AnalyticsResponse(BaseModel):
    total_apps: int
    total_responded: int
    response_rate: float
    country_rates: dict
    ats_correlation: dict
    computed_at: str


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["infra"])
async def health():
    """Liveness + readiness probe."""
    checks = {}

    # DB check
    try:
        from infra.db.session import get_db
        async with get_db() as session:
            await session.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"

    # Redis check
    try:
        import redis.asyncio as aioredis
        r = await aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.close()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    status = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks, "ts": datetime.now(timezone.utc).isoformat()}


# ── Applications ──────────────────────────────────────────────────────────────

@app.get("/api/applications", response_model=list[ApplicationSummary], tags=["applications"])
async def list_applications(
    status: str | None = Query(None),
    country: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    from infra.db.session import get_db
    from infra.db.models import ApplicationRecordORM
    from sqlalchemy import select

    async with get_db() as session:
        q = select(ApplicationRecordORM).order_by(
            ApplicationRecordORM.last_updated.desc()
        ).limit(limit).offset(offset)
        if status:
            q = q.where(ApplicationRecordORM.status == status)
        if country:
            q = q.where(ApplicationRecordORM.listing_country == country)
        result = await session.execute(q)
        rows = result.scalars().all()

    return [
        ApplicationSummary(
            id=r.id,
            company=r.listing_company,
            title=r.listing_title,
            country=r.listing_country,
            portal=r.listing_portal,
            status=r.status,
            ats_score=r.ats_score,
            submitted_at=r.submitted_at.isoformat() if r.submitted_at else None,
            confirmation_id=r.confirmation_id,
        )
        for r in rows
    ]


@app.get("/api/applications/{app_id}", tags=["applications"])
async def get_application(app_id: str):
    from infra.db.session import get_db
    from infra.db.models import ApplicationRecordORM

    async with get_db() as session:
        row = await session.get(ApplicationRecordORM, app_id)
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    return row.__dict__


@app.post("/api/applications/{app_id}/approve", tags=["applications"])
async def approve_application(app_id: str):
    """Human approves a PENDING_HUMAN application — dispatches to Celery for submission."""
    from infra.db.session import get_db
    from infra.db.models import ApplicationRecordORM
    from infra.celery_app import fill_and_submit

    async with get_db() as session:
        row = await session.get(ApplicationRecordORM, app_id)
        if not row:
            raise HTTPException(404, "Not found")
        if row.status != ApplicationStatus.PENDING_HUMAN.value:
            raise HTTPException(400, f"Cannot approve: status is '{row.status}'")
        row.status = ApplicationStatus.SUBMITTING.value if hasattr(ApplicationStatus, 'SUBMITTING') else "submitting"
        await session.commit()

    # Dispatch to Celery apply queue
    task = fill_and_submit.apply_async(
        args=[{"id": app_id}, {"dry_run": False}],
        queue="apply",
    )
    await ws_manager.broadcast({"event": "approved", "id": app_id, "task_id": task.id})
    return {"status": "dispatched", "task_id": task.id}


@app.post("/api/applications/{app_id}/skip", tags=["applications"])
async def skip_application(app_id: str):
    """Human discards a pending application."""
    from infra.db.session import get_db
    from infra.db.models import ApplicationRecordORM

    async with get_db() as session:
        row = await session.get(ApplicationRecordORM, app_id)
        if not row:
            raise HTTPException(404, "Not found")
        row.status = "skipped"
        await session.commit()

    await ws_manager.broadcast({"event": "skipped", "id": app_id})
    return {"status": "skipped"}


# ── Human review queue ────────────────────────────────────────────────────────

@app.get("/api/queue/human", tags=["queue"])
async def human_queue():
    """All applications awaiting human review."""
    from infra.db.session import get_db
    from infra.db.models import ApplicationRecordORM
    from sqlalchemy import select

    async with get_db() as session:
        q = select(ApplicationRecordORM).where(
            ApplicationRecordORM.status == ApplicationStatus.PENDING_HUMAN.value
        ).order_by(ApplicationRecordORM.created_at)
        result = await session.execute(q)
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "company": r.listing_company,
            "title": r.listing_title,
            "country": r.listing_country,
            "ats_score": r.ats_score,
            "retry_count": r.retry_count,
            "error_log": r.error_log,
            "url": r.listing_url,
        }
        for r in rows
    ]


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.get("/api/analytics", tags=["analytics"])
async def get_analytics():
    from infra.db.session import get_db
    from infra.db.models import AnalyticsSnapshotORM
    from sqlalchemy import select

    async with get_db() as session:
        q = select(AnalyticsSnapshotORM).order_by(
            AnalyticsSnapshotORM.computed_at.desc()
        ).limit(1)
        result = await session.execute(q)
        row = result.scalar_one_or_none()

    if not row:
        return {"message": "No analytics yet. Run at least 5 applications first."}

    return {
        "total_apps": row.total_apps,
        "total_responded": row.total_responded,
        "response_rate": row.response_rate,
        "country_rates": row.country_rates,
        "ats_correlation": row.ats_correlation,
        "updated_weights": row.updated_weights,
        "computed_at": row.computed_at.isoformat(),
    }


# ── Platforms ─────────────────────────────────────────────────────────────────

@app.get("/api/platforms", tags=["platforms"])
async def list_platforms():
    from agents.job_discovery.platforms import PLATFORMS
    return [
        {
            "id": p.id,
            "name": p.name,
            "countries": [c.value for c in p.countries[:5]],
            "domains": p.domains,
            "requires_auth": p.requires_auth,
            "rate_limit_sec": p.rate_limit_sec,
            "notes": p.notes[:100],
        }
        for p in PLATFORMS
    ]


# ── Trigger run ───────────────────────────────────────────────────────────────

@app.post("/api/run", tags=["pipeline"])
async def trigger_run(req: RunRequest):
    """Trigger a full discovery + apply cycle asynchronously via Celery."""
    from infra.celery_app import PipelineDispatcher
    from agents.job_discovery.platforms import PLATFORMS
    from config.settings import settings

    prefs = req.prefs_override or {"target_countries": ["usa", "germany"], "dry_run": True}
    platform_ids = [p.id for p in PLATFORMS[:5]]  # start with top 5
    queries = ["research internship ML", "research internship NLP"]

    task_ids = PipelineDispatcher.dispatch_discovery(
        platform_ids=platform_ids,
        queries=queries,
        countries=prefs.get("target_countries", ["usa"]),
        master_resume_json={},
        prefs_json=prefs,
    )

    await ws_manager.broadcast({"event": "run_started", "tasks": len(task_ids)})
    return {"dispatched": len(task_ids), "task_ids": task_ids[:10]}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    """Real-time status updates pushed to dashboard."""
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json({"event": "connected", "ts": datetime.now(timezone.utc).isoformat()})
        while True:
            # Keep alive ping every 30s
            await asyncio.sleep(30)
            await websocket.send_json({"event": "ping"})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, tags=["dashboard"])
async def dashboard():
    from api.dashboard import DASHBOARD_HTML
    return HTMLResponse(content=DASHBOARD_HTML)
