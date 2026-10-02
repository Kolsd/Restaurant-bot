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

    # Business counts. They used a bare pool.acquire() on RLS tables, which as
    # mesio_app (prod) reads zero rows: "0 orders today" every day.
    from app.repositories.internal import platform_stats_repo  # noqa: PLC0415
    from app.services.hq_snapshot import DEFAULT_TZ, local_day_start_utc  # noqa: PLC0415
    try:
        with bypass_tenant_scope("internal_ops_metrics_cross_tenant"):
            metrics.update(await platform_stats_repo.db_ops_counts(local_day_start_utc(DEFAULT_TZ)))
    except Exception as exc:
        log.exception("ops.metrics.business_counts_error", exc_type=type(exc).__name__)
        for key in ("orders_today", "active_table_sessions", "active_diners", "restaurants_total", "errors_1h"):
            metrics[key] = None

    return metrics
