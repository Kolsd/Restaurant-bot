"""
app/routes/health.py

Lightweight liveness + readiness probe used by Railway as healthcheck.
GET /health  →  200 {"status": "ok", "db": "ok"}
             →  503 {"status": "degraded", "db": "error: <ExcType>"}  if DB is unreachable

NOTE: /monitoring (page) and /health/metrics have been moved to
      app/routes/internal/ops.py under /internal/monitoring and /api/internal/ops/metrics.
"""

from fastapi import APIRouter
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
    except Exception:
        # Log the full exception server-side for diagnostics; never expose the
        # exception type or message to callers (prevents technology fingerprinting).
        log.exception("health.db_check_failed")
        checks["db"] = "error"
        checks["status"] = "degraded"
        return JSONResponse(status_code=503, content=checks)
    return checks
