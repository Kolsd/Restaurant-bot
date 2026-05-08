"""
app/routes/internal/ops.py

Operational observability endpoints for Mesio internal use only.
NOT to be exposed to restaurant users.

Endpoints:
  GET /internal/monitoring       → monitoring dashboard HTML page
  GET /api/internal/ops/metrics  → detailed operational metrics (requires ADMIN_KEY)
"""

import time

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from app.services.database import get_pool
from app.services.logging import get_logger
from app.routes.deps import verify_superadmin
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(tags=["internal-ops"])


@router.get("/internal/monitoring")
async def monitoring_page():
    from fastapi.responses import FileResponse
    return FileResponse("app/static/html/internal/monitoring.html")


@router.get("/api/internal/ops/metrics")
async def health_metrics(_: None = Depends(verify_superadmin)):
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
        log.exception("ops.metrics.pool_error", exc_type=type(exc).__name__)
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
        log.exception("ops.metrics.inbox_depth_error", exc_type=type(exc).__name__)
        metrics["inbox_queue_depth"] = None

    # Dead letters
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metrics["inbox_dead_letters"] = await conn.fetchval(
                "SELECT COUNT(*) FROM webhook_inbox WHERE last_error LIKE 'DEAD_LETTER:%'"
            )
    except Exception as exc:
        log.exception("ops.metrics.dead_letters_error", exc_type=type(exc).__name__)
        metrics["inbox_dead_letters"] = None

    # Inbox worker processing metrics (in-process, not from DB)
    try:
        from app.services.inbox_worker import get_metrics as _get_worker_metrics
        metrics.update(_get_worker_metrics())
    except Exception as exc:
        log.exception("ops.metrics.worker_metrics_error", exc_type=type(exc).__name__)

    # Scheduler heartbeat
    try:
        from app.services import state_store as _ss
        last_tick = await _ss.get_scheduler_heartbeat()
        now_ts = int(time.time())
        age = (now_ts - last_tick) if last_tick is not None else None
        metrics["scheduler"] = {
            "last_tick_at": last_tick,
            "last_tick_age_seconds": age,
            "healthy": (age is not None and age < 90),
        }
    except Exception as exc:
        log.exception("ops.metrics.scheduler_heartbeat_error", exc_type=type(exc).__name__)
        metrics["scheduler"] = {"last_tick_at": None, "last_tick_age_seconds": None, "healthy": False}

    with bypass_tenant_scope("internal_ops_metrics_cross_tenant"):
        # Orders created today
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                metrics["orders_today"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE created_at::date = CURRENT_DATE"
                )
        except Exception as exc:
            log.exception("ops.metrics.orders_today_error", exc_type=type(exc).__name__)
            metrics["orders_today"] = None

        # Open table sessions right now
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                metrics["active_table_sessions"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM table_sessions WHERE closed_at IS NULL"
                )
        except Exception as exc:
            log.exception("ops.metrics.active_table_sessions_error", exc_type=type(exc).__name__)
            metrics["active_table_sessions"] = None

        # Conversations with activity in last 30 minutes
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                metrics["active_conversations"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations WHERE updated_at > NOW() - INTERVAL '30 minutes'"
                )
        except Exception as exc:
            log.exception("ops.metrics.active_conversations_error", exc_type=type(exc).__name__)
            metrics["active_conversations"] = None

        # Total restaurant count
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                metrics["restaurants_total"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM restaurants"
                )
        except Exception as exc:
            log.exception("ops.metrics.restaurants_total_error", exc_type=type(exc).__name__)
            metrics["restaurants_total"] = None

        # Staff currently on shift
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                metrics["staff_clocked_in"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM staff_shifts WHERE clock_out IS NULL"
                )
        except Exception as exc:
            log.exception("ops.metrics.staff_clocked_in_error", exc_type=type(exc).__name__)
            metrics["staff_clocked_in"] = None

    return metrics
