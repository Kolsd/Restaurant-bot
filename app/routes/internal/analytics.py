"""
app/routes/analytics.py

Platform-level revenue / product analytics for Mesio superadmin.

Endpoints (all require Authorization: Bearer <ADMIN_KEY>):
  GET /analytics               → analytics.html dashboard page
  GET /api/analytics/overview  → aggregate platform KPIs
  GET /api/analytics/restaurants → per-restaurant breakdown + onboarding score
  GET /api/analytics/trends    → daily order & conversation counts (last 30 days)
"""

import os
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app.services.database import get_pool
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["analytics"])

# ── Auth helper ───────────────────────────────────────────────────────────────

def _check_admin_key(request: Request) -> bool:
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        return False
    auth_header = request.headers.get("Authorization", "")
    provided = auth_header.removeprefix("Bearer ").strip()
    return provided == admin_key


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/internal/analytics")
async def analytics_page():
    return FileResponse("app/static/html/internal/analytics.html")


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/overview")
async def analytics_overview(request: Request):
    if not _check_admin_key(request):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    pool = await get_pool()
    result: dict = {}

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
            restaurants["active_7d"] = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT bot_number)
                FROM conversations
                WHERE updated_at > NOW() - INTERVAL '7 days'
                """
            )
    except Exception as exc:
        log.exception("analytics.overview.active_7d", exc_type=type(exc).__name__)
        restaurants["active_7d"] = None

    try:
        async with pool.acquire() as conn:
            restaurants["active_30d"] = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT bot_number)
                FROM conversations
                WHERE updated_at > NOW() - INTERVAL '30 days'
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

@router.get("/api/internal/analytics/restaurants")
async def analytics_restaurants(request: Request):
    if not _check_admin_key(request):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    pool = await get_pool()

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    r.id,
                    r.name,
                    r.whatsapp_number,
                    r.created_at::date                                   AS created_at,
                    r.menu,
                    r.features,
                    COALESCE(o30.cnt, 0)                                 AS orders_30d,
                    COALESCE(c30.cnt, 0)                                 AS conversations_30d,
                    o_last.last_order_at::date                           AS last_order_at,
                    c_last.last_conversation_at::date                    AS last_conversation_at,
                    COALESCE(s.staff_count, 0)                           AS staff_count,
                    CASE WHEN o_last.last_order_at IS NOT NULL THEN 1 ELSE 0 END AS has_any_order
                FROM restaurants r
                LEFT JOIN (
                    SELECT bot_number, COUNT(*) AS cnt
                    FROM orders
                    WHERE created_at > NOW() - INTERVAL '30 days'
                    GROUP BY bot_number
                ) o30 ON o30.bot_number = r.whatsapp_number
                LEFT JOIN (
                    SELECT bot_number, COUNT(*) AS cnt
                    FROM conversations
                    WHERE updated_at > NOW() - INTERVAL '30 days'
                    GROUP BY bot_number
                ) c30 ON c30.bot_number = r.whatsapp_number
                LEFT JOIN (
                    SELECT bot_number, MAX(created_at) AS last_order_at
                    FROM orders
                    GROUP BY bot_number
                ) o_last ON o_last.bot_number = r.whatsapp_number
                LEFT JOIN (
                    SELECT bot_number, MAX(updated_at) AS last_conversation_at
                    FROM conversations
                    GROUP BY bot_number
                ) c_last ON c_last.bot_number = r.whatsapp_number
                LEFT JOIN (
                    SELECT restaurant_id, COUNT(*) AS staff_count
                    FROM staff
                    GROUP BY restaurant_id
                ) s ON s.restaurant_id = r.id
                ORDER BY r.created_at DESC
                """
            )
    except Exception as exc:
        log.exception("analytics.restaurants.query_error", exc_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to fetch restaurant analytics"}
        )

    restaurants_list = []
    for row in rows:
        features = row["features"] or {}
        menu = row["menu"]

        # Onboarding score — 5 steps, 20% each
        has_menu = bool(menu and (isinstance(menu, list) and len(menu) > 0 or isinstance(menu, dict) and menu))
        has_staff = row["staff_count"] > 0
        has_billing = bool(
            features.get("billing_provider")
            or features.get("alegra_email")
            or features.get("billing_enabled")
        )
        has_whatsapp = bool(row["whatsapp_number"])
        has_orders = bool(row["has_any_order"])

        score = sum([has_menu, has_staff, has_billing, has_whatsapp, has_orders]) * 20

        restaurants_list.append({
            "id": row["id"],
            "name": row["name"],
            "whatsapp_number": row["whatsapp_number"],
            "created_at": str(row["created_at"]) if row["created_at"] else None,
            "orders_30d": row["orders_30d"],
            "conversations_30d": row["conversations_30d"],
            "last_order_at": str(row["last_order_at"]) if row["last_order_at"] else None,
            "last_conversation_at": str(row["last_conversation_at"]) if row["last_conversation_at"] else None,
            "has_billing": has_billing,
            "has_menu": has_menu,
            "staff_count": row["staff_count"],
            "onboarding_score": score,
            # Breakdown for UI detail
            "_setup": {
                "menu": has_menu,
                "staff": has_staff,
                "billing": has_billing,
                "whatsapp": has_whatsapp,
                "orders": has_orders,
            },
        })

    return {"restaurants": restaurants_list}


# ── Trends ────────────────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/trends")
async def analytics_trends(request: Request):
    if not _check_admin_key(request):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    pool = await get_pool()
    result: dict = {}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT created_at::date AS date, COUNT(*) AS count
                FROM orders
                WHERE created_at > NOW() - INTERVAL '30 days'
                GROUP BY 1
                ORDER BY 1
                """
            )
        result["daily_orders"] = [
            {"date": str(row["date"]), "count": row["count"]}
            for row in rows
        ]
    except Exception as exc:
        log.exception("analytics.trends.orders_error", exc_type=type(exc).__name__)
        result["daily_orders"] = []

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT updated_at::date AS date, COUNT(*) AS count
                FROM conversations
                WHERE updated_at > NOW() - INTERVAL '30 days'
                GROUP BY 1
                ORDER BY 1
                """
            )
        result["daily_conversations"] = [
            {"date": str(row["date"]), "count": row["count"]}
            for row in rows
        ]
    except Exception as exc:
        log.exception("analytics.trends.conversations_error", exc_type=type(exc).__name__)
        result["daily_conversations"] = []

    return result
