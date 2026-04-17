"""
app/routes/health.py

Liveness + readiness probes used by Railway and upstream monitoring.

GET /health       → process-only liveness probe.
                    NEVER checks DB or Redis.  Returns 200 {"status": "ok"}
                    as long as the Python process is alive.  This is the
                    endpoint Railway configures as its healthcheck — it must
                    not trip during a transient DB/Redis outage or the web
                    service gets restarted, compounding the problem.

GET /health/deep  → full dependency check (DB + Redis).
                    Always returns HTTP 200 so Railway does NOT use it as a
                    restart trigger.  The body surfaces per-dependency status
                    so monitoring dashboards can page ops without causing
                    Railway service restarts.
                    Body: {"status": "ok"|"degraded", "db": "ok"|"error",
                           "redis": "ok"|"unavailable"|"error"}

NOTE: /api/internal/ops/metrics (admin-key protected, DB metrics) is in
      app/routes/internal/ops.py — this file is intentionally import-free
      from that module.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.services.database import get_pool  # top-level import — needed for test patching
from app.services.redis_client import get_redis  # top-level import — needed for test patching
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """Process-only liveness probe.

    Returns 200 {"status": "ok"} as long as the Python process is alive.
    Does NOT touch DB or Redis — Railway healthcheck points here so a
    transient DB outage never causes an unnecessary service restart.
    """
    return {"status": "ok"}


@router.get("/health/deep")
async def health_deep():
    """Full dependency health check (DB + Redis).

    Always returns HTTP 200 — the body carries per-dependency status.
    Keeping the status code at 200 ensures Railway does not treat a
    degraded dependency as a reason to restart the web service.

    Callers (Grafana, Slack alerts, manual checks) should inspect the
    body ``status`` field to detect degradation.
    """
    checks: dict = {"status": "ok", "db": "ok", "redis": "ok"}

    # ── DB check ─────────────────────────────────────────────────────────────
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        log.exception("health.deep.db_check_failed")
        checks["db"] = "error"
        checks["status"] = "degraded"

    # ── Redis check ──────────────────────────────────────────────────────────
    try:
        redis = await get_redis()
        if redis is not None:
            await redis.ping()
        else:
            checks["redis"] = "unavailable"
    except Exception:
        log.exception("health.deep.redis_check_failed")
        checks["redis"] = "error"
        if checks["status"] != "degraded":
            checks["status"] = "degraded"

    # Always 200 — Railway must not restart on dependency degradation.
    return JSONResponse(status_code=200, content=checks)
