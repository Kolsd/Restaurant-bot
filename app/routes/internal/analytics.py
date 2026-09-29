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

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.services.database import get_pool
from app.services.logging import get_logger
from app.routes.deps import verify_superadmin
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(tags=["analytics"])


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/overview")
async def analytics_overview(_: None = Depends(verify_superadmin)):

    pool = await get_pool()
    result: dict = {}

    with bypass_tenant_scope("internal_analytics_cross_tenant"):

        # ── Restaurants ───────────────────────────────────────────────────────────
        restaurants: dict = {}
        try:
            async with pool.acquire() as conn:
                restaurants["total"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM restaurants"
                )
        except Exception as exc:
            log.exception("analytics.overview.restaurants_total", exc_type=type(exc).__name__)
            restaurants["total"] = None

        try:
            async with pool.acquire() as conn:
                # Count distinct orgs (not locations) that had conversation activity.
                # A multi-sede org should count once, not once per location.
                restaurants["active_7d"] = await conn.fetchval(
                    """
                    WITH bot_orgs AS (
                        SELECT
                            l.org_id,
                            COALESCE(l.whatsapp_number, o.whatsapp_number, 'web' || o.id::text) AS bot_number
                        FROM locations l
                        JOIN organizations o ON o.id = l.org_id
                        WHERE COALESCE(l.whatsapp_number, o.whatsapp_number, 'web' || o.id::text) IS NOT NULL
                    )
                    SELECT COUNT(DISTINCT bo.org_id)
                    FROM conversations c
                    JOIN bot_orgs bo ON bo.bot_number = c.bot_number
                    WHERE c.updated_at > NOW() - INTERVAL '7 days'
                    """
                )
        except Exception as exc:
            log.exception("analytics.overview.active_7d", exc_type=type(exc).__name__)
            restaurants["active_7d"] = None

        try:
            async with pool.acquire() as conn:
                # Same org-level dedup for 30-day window.
                restaurants["active_30d"] = await conn.fetchval(
                    """
                    WITH bot_orgs AS (
                        SELECT
                            l.org_id,
                            COALESCE(l.whatsapp_number, o.whatsapp_number, 'web' || o.id::text) AS bot_number
                        FROM locations l
                        JOIN organizations o ON o.id = l.org_id
                        WHERE COALESCE(l.whatsapp_number, o.whatsapp_number, 'web' || o.id::text) IS NOT NULL
                    )
                    SELECT COUNT(DISTINCT bo.org_id)
                    FROM conversations c
                    JOIN bot_orgs bo ON bo.bot_number = c.bot_number
                    WHERE c.updated_at > NOW() - INTERVAL '30 days'
                    """
                )
        except Exception as exc:
            log.exception("analytics.overview.active_30d", exc_type=type(exc).__name__)
            restaurants["active_30d"] = None

        try:
            async with pool.acquire() as conn:
                restaurants["new_this_week"] = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM restaurants
                    WHERE created_at > NOW() - INTERVAL '7 days'
                    """
                )
        except Exception as exc:
            log.exception("analytics.overview.new_this_week", exc_type=type(exc).__name__)
            restaurants["new_this_week"] = None

        try:
            async with pool.acquire() as conn:
                restaurants["new_this_month"] = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM restaurants
                    WHERE created_at > date_trunc('month', NOW())
                    """
                )
        except Exception as exc:
            log.exception("analytics.overview.new_this_month", exc_type=type(exc).__name__)
            restaurants["new_this_month"] = None

        result["restaurants"] = restaurants

        # ── Orders ────────────────────────────────────────────────────────────────
        orders: dict = {}
        try:
            async with pool.acquire() as conn:
                orders["today"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE created_at::date = CURRENT_DATE"
                )
        except Exception as exc:
            log.exception("analytics.overview.orders_today", exc_type=type(exc).__name__)
            orders["today"] = None

        try:
            async with pool.acquire() as conn:
                orders["this_week"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE created_at > NOW() - INTERVAL '7 days'"
                )
        except Exception as exc:
            log.exception("analytics.overview.orders_week", exc_type=type(exc).__name__)
            orders["this_week"] = None

        try:
            async with pool.acquire() as conn:
                orders["this_month"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE created_at > date_trunc('month', NOW())"
                )
        except Exception as exc:
            log.exception("analytics.overview.orders_month", exc_type=type(exc).__name__)
            orders["this_month"] = None

        try:
            async with pool.acquire() as conn:
                avg = await conn.fetchval(
                    """
                    SELECT ROUND(COUNT(*)::numeric / 30, 1)
                    FROM orders
                    WHERE created_at > NOW() - INTERVAL '30 days'
                    """
                )
                orders["avg_daily_30d"] = float(avg) if avg is not None else None  # JSON boundary
        except Exception as exc:
            log.exception("analytics.overview.orders_avg_daily", exc_type=type(exc).__name__)
            orders["avg_daily_30d"] = None

        result["orders"] = orders

        # ── Conversations ─────────────────────────────────────────────────────────
        conversations: dict = {}
        try:
            async with pool.acquire() as conn:
                conversations["today"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations WHERE updated_at::date = CURRENT_DATE"
                )
        except Exception as exc:
            log.exception("analytics.overview.conversations_today", exc_type=type(exc).__name__)
            conversations["today"] = None

        try:
            async with pool.acquire() as conn:
                conversations["this_week"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations WHERE updated_at > NOW() - INTERVAL '7 days'"
                )
        except Exception as exc:
            log.exception("analytics.overview.conversations_week", exc_type=type(exc).__name__)
            conversations["this_week"] = None

        try:
            async with pool.acquire() as conn:
                conversations["active_now"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversations WHERE updated_at > NOW() - INTERVAL '30 minutes'"
                )
        except Exception as exc:
            log.exception("analytics.overview.conversations_active_now", exc_type=type(exc).__name__)
            conversations["active_now"] = None

        result["conversations"] = conversations

        # ── Billing ───────────────────────────────────────────────────────────────
        billing: dict = {}
        try:
            async with pool.acquire() as conn:
                billing["configured_count"] = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM restaurants
                    WHERE features->>'billing_provider' IS NOT NULL
                    """
                )
        except Exception as exc:
            log.exception("analytics.overview.billing_configured", exc_type=type(exc).__name__)
            billing["configured_count"] = None

        try:
            async with pool.acquire() as conn:
                billing["invoices_today"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM fiscal_invoices WHERE created_at::date = CURRENT_DATE"
                )
        except Exception as exc:
            log.exception("analytics.overview.invoices_today", exc_type=type(exc).__name__)
            billing["invoices_today"] = None

        try:
            async with pool.acquire() as conn:
                billing["invoices_this_month"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM fiscal_invoices WHERE created_at > date_trunc('month', NOW())"
                )
        except Exception as exc:
            log.exception("analytics.overview.invoices_month", exc_type=type(exc).__name__)
            billing["invoices_this_month"] = None

        result["billing"] = billing

    return result


# ── Per-restaurant breakdown ──────────────────────────────────────────────────

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
