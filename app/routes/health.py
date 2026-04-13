"""
app/routes/health.py

Lightweight liveness + readiness probe.
GET /health         →  200 {"status": "ok", "db": "ok"}
                    →  503 {"status": "degraded", "db": "error: <ExcType>"}  if DB is unreachable
GET /health/metrics →  200 operational metrics (inbox queue depth, pool stats)
                    →  401 if Authorization: Bearer <ADMIN_KEY> header is missing or wrong
"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.services.database import get_pool  # top-level import — needed for test patching
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    checks: dict = {"status": "ok", "db": "ok"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        checks["db"] = f"error: {type(exc).__name__}"
        checks["status"] = "degraded"
        return JSONResponse(status_code=503, content=checks)
    return checks


@router.get("/monitoring")
async def monitoring_page():
    from fastapi.responses import FileResponse
    return FileResponse("app/static/html/monitoring.html")


@router.get("/health/metrics")
async def health_metrics(request: Request):
    admin_key = os.environ.get("ADMIN_KEY", "")
    auth_header = request.headers.get("Authorization", "")
    provided_key = auth_header.removeprefix("Bearer ").strip()

    if not admin_key or provided_key != admin_key:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    metrics: dict = {}

    # Pool stats — may work even if queries fail
    try:
        pool = await get_pool()
        pool_size = pool.get_size()
        pool_idle = pool.get_idle_size()
        metrics["db_pool_size"] = pool_size
        metrics["db_pool_free"] = pool_idle
        metrics["db_pool_used"] = pool_size - pool_idle
    except Exception as exc:
        log.exception("health.metrics.pool_error", exc_type=type(exc).__name__)
        metrics["db_pool_size"] = None
        metrics["db_pool_free"] = None
        metrics["db_pool_used"] = None

    # Inbox queue depth
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["inbox_queue_depth"] = await conn.fetchval(
                "SELECT COUNT(*) FROM webhook_inbox WHERE processed_at IS NULL"
            )
    except Exception as exc:
        log.exception("health.metrics.inbox_depth_error", exc_type=type(exc).__name__)
        metrics["inbox_queue_depth"] = None

    # Dead letters
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["inbox_dead_letters"] = await conn.fetchval(
                "SELECT COUNT(*) FROM webhook_inbox WHERE last_error LIKE 'DEAD_LETTER:%'"
            )
    except Exception as exc:
        log.exception("health.metrics.dead_letters_error", exc_type=type(exc).__name__)
        metrics["inbox_dead_letters"] = None

    # Inbox worker processing metrics (in-process, not from DB)
    try:
        from app.services.inbox_worker import get_metrics as _get_worker_metrics
        metrics.update(_get_worker_metrics())
    except Exception as exc:
        log.exception("health.metrics.worker_metrics_error", exc_type=type(exc).__name__)

    # Orders created today
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["orders_today"] = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE created_at::date = CURRENT_DATE"
            )
    except Exception as exc:
        log.exception("health.metrics.orders_today_error", exc_type=type(exc).__name__)
        metrics["orders_today"] = None

    # Open table sessions right now
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["active_table_sessions"] = await conn.fetchval(
                "SELECT COUNT(*) FROM table_sessions WHERE closed_at IS NULL"
            )
    except Exception as exc:
        log.exception("health.metrics.active_table_sessions_error", exc_type=type(exc).__name__)
        metrics["active_table_sessions"] = None

    # Conversations with activity in last 30 minutes
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["active_conversations"] = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE updated_at > NOW() - INTERVAL '30 minutes'"
            )
    except Exception as exc:
        log.exception("health.metrics.active_conversations_error", exc_type=type(exc).__name__)
        metrics["active_conversations"] = None

    # Total restaurant count
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["restaurants_total"] = await conn.fetchval(
                "SELECT COUNT(*) FROM restaurants"
            )
    except Exception as exc:
        log.exception("health.metrics.restaurants_total_error", exc_type=type(exc).__name__)
        metrics["restaurants_total"] = None

    # Staff currently on shift
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["staff_clocked_in"] = await conn.fetchval(
                "SELECT COUNT(*) FROM staff_shifts WHERE clock_out IS NULL"
            )
    except Exception as exc:
        log.exception("health.metrics.staff_clocked_in_error", exc_type=type(exc).__name__)
        metrics["staff_clocked_in"] = None

    return metrics
