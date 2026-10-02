"""
app/routes/internal/analytics.py

The three platform KPIs the HQ home (/internal) shows. The analytics page,
per-restaurant breakdown, CSV exports, trends, activation funnel and churn
lists were deleted in the 2026-09 cleanup.

Endpoints (all require Authorization: Bearer <ADMIN_KEY>):
  GET /api/internal/analytics/overview        → aggregate platform KPIs
  GET /api/internal/analytics/mrr             → MRR + month-over-month delta
  GET /api/internal/analytics/orders-rescued  → north-star, cross-tenant
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.services.logging import get_logger
from app.routes.deps import verify_superadmin
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(tags=["analytics"])


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/overview")
async def analytics_overview(_: None = Depends(verify_superadmin)):
    """Platform KPIs for the HQ home: sales (table rounds + web orders),
    restaurants selling, diners, errors and open alerts — demo excluded.

    "Today" is Bogotá's business day, like the restaurants' own dashboards.
    """
    from app.repositories.internal import platform_stats_repo  # noqa: PLC0415
    from app.services.hq_snapshot import DEFAULT_TZ, local_day_start_utc  # noqa: PLC0415

    with bypass_tenant_scope("internal_analytics_cross_tenant"):
        data = await platform_stats_repo.db_platform_overview(local_day_start_utc(DEFAULT_TZ))
    for key in ("sales_today", "sales_7d"):
        data["orders"][key] = float(data["orders"][key])  # JSON boundary
    return data


# ── MRR ───────────────────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/mrr")
async def analytics_mrr(_: None = Depends(verify_superadmin)):
    """Current MRR + MoM delta. Cross-tenant — runs under bypass_tenant_scope."""
    from app.repositories.internal import mrr_repo

    with bypass_tenant_scope("internal_mrr_query"):
        try:
            current = await mrr_repo.db_compute_mrr()
            delta = await mrr_repo.db_compute_mrr_delta()
        except Exception as exc:
            log.exception("analytics.mrr.error", exc_type=type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "Failed to compute MRR"})

    return {**current, **delta}


# ── North-star: Rescued Orders ────────────────────────────────────────────

@router.get("/api/internal/analytics/orders-rescued")
async def analytics_rescued_orders(
    period: str = "mtd",
    _: None = Depends(verify_superadmin),
):
    """
    Cross-tenant north-star metric — Rescued Orders.

    Returns total bot-originated orders across all tenants + per-tenant ranking.

    period values:
      mtd  — month-to-date (1st of current month → today)
      30d  — last 30 days

    Response:
      {
        "period": "mtd",
        "total": 1843,
        "ranking": [
          {"org_id": 5, "org_name": "La Parrilla", "count": 312, "delivery": 200, "table": 112},
          ...
        ]
      }
    """
    from datetime import date, timedelta
    from app.repositories.north_star_repo import db_count_rescued_orders_global

    today = date.today()
    if period == "mtd":
        period_start = today.replace(day=1)
        period_end   = today
    else:  # 30d default
        period_start = today - timedelta(days=29)
        period_end   = today

    with bypass_tenant_scope("internal_analytics_rescued_orders_cross_tenant"):
        try:
            ranking = await db_count_rescued_orders_global(period_start, period_end)
        except Exception as exc:
            log.exception("analytics.rescued_orders_failed")
            return JSONResponse(status_code=500, content={"detail": "Error al calcular pedidos rescatados"})

    total = sum(r["count"] for r in ranking)
    return {
        "period": period,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total": total,
        "ranking": ranking,
    }
