"""
app/routes/analytics.py

Platform-level revenue / product analytics for Mesio superadmin.

Endpoints (all require Authorization: Bearer <ADMIN_KEY>):
  GET /analytics               → analytics.html dashboard page
  GET /api/analytics/overview  → aggregate platform KPIs
  GET /api/analytics/restaurants → per-restaurant breakdown + onboarding score
  GET /api/analytics/trends    → daily order & conversation counts (last 30 days)
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import FileResponse, JSONResponse

from app.services.database import get_pool
from app.services.logging import get_logger
from app.routes.deps import verify_superadmin
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(tags=["analytics"])


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/internal/analytics")
async def analytics_page():
    return FileResponse("app/static/html/internal/analytics.html")


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
                            COALESCE(l.whatsapp_number, o.whatsapp_number) AS bot_number
                        FROM locations l
                        JOIN organizations o ON o.id = l.org_id
                        WHERE COALESCE(l.whatsapp_number, o.whatsapp_number) IS NOT NULL
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
                            COALESCE(l.whatsapp_number, o.whatsapp_number) AS bot_number
                        FROM locations l
                        JOIN organizations o ON o.id = l.org_id
                        WHERE COALESCE(l.whatsapp_number, o.whatsapp_number) IS NOT NULL
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

@router.get("/api/internal/analytics/restaurants")
async def analytics_restaurants(_: None = Depends(verify_superadmin)):

    pool = await get_pool()

    with bypass_tenant_scope("internal_analytics_cross_tenant"):
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
                        SELECT org_id, COUNT(*) AS staff_count
                        FROM staff
                        GROUP BY org_id
                    ) s ON s.org_id = r.id
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
async def analytics_trends(_: None = Depends(verify_superadmin)):

    pool = await get_pool()
    result: dict = {}

    with bypass_tenant_scope("internal_analytics_cross_tenant"):
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


# ── Activation funnel ─────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/activation")
async def analytics_activation(_: None = Depends(verify_superadmin)):
    """Return activation funnel: signed_up → menu_loaded → first_message → first_order.

    Aggregate counts + conversion percentages + median/p75 time-to-stage in hours
    + per-org breakdown with stage timestamps.
    """
    pool = await get_pool()

    with bypass_tenant_scope("internal_activation_funnel_cross_tenant"):
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH bot_orgs AS (
                        -- Resolve bot_number → org via locations override OR org fallback
                        -- (locations.whatsapp_number can be NULL; menu lives on organizations).
                        SELECT
                            l.org_id,
                            COALESCE(l.whatsapp_number, o.whatsapp_number) AS bot_number
                        FROM locations l
                        JOIN organizations o ON o.id = l.org_id
                        WHERE COALESCE(l.whatsapp_number, o.whatsapp_number) IS NOT NULL
                    ),
                    org_menu AS (
                        -- Stage 2: org has at least one dish in any category
                        -- (menu is on organizations, not locations)
                        SELECT id AS org_id
                        FROM organizations
                        WHERE
                            menu IS NOT NULL
                            AND menu != '{}'::jsonb
                            AND jsonb_typeof(menu) = 'object'
                            AND (SELECT COUNT(*) FROM jsonb_each(menu)) > 0
                    ),
                    org_first_message AS (
                        -- Stage 3: first real customer conversation per org
                        SELECT
                            bo.org_id,
                            MIN(c.created_at) AS first_message_at
                        FROM conversations c
                        JOIN bot_orgs bo ON bo.bot_number = c.bot_number
                        GROUP BY bo.org_id
                    ),
                    org_first_order AS (
                        -- Stage 4: first closed delivery order per org
                        SELECT
                            bo.org_id,
                            MIN(o.created_at) AS first_order_at
                        FROM orders o
                        JOIN bot_orgs bo ON bo.bot_number = o.bot_number
                        WHERE o.status IN ('entregado', 'factura_entregada')
                        GROUP BY bo.org_id
                    ),
                    org_first_table_order AS (
                        -- Stage 4 (salon path): first closed table order per org
                        SELECT
                            ts.org_id,
                            MIN(to2.created_at) AS first_order_at
                        FROM table_orders to2
                        JOIN table_sessions ts ON ts.id = to2.session_id
                        WHERE to2.status IN ('entregado', 'factura_entregada')
                        GROUP BY ts.org_id
                    ),
                    org_first_any_order AS (
                        SELECT org_id, MIN(first_order_at) AS first_order_at
                        FROM (
                            SELECT org_id, first_order_at FROM org_first_order
                            UNION ALL
                            SELECT org_id, first_order_at FROM org_first_table_order
                        ) combined
                        GROUP BY org_id
                    )
                    SELECT
                        o.id                                              AS org_id,
                        o.name                                            AS org_name,
                        o.created_at                                      AS signed_up_at,
                        CASE WHEN om.org_id IS NOT NULL THEN TRUE ELSE FALSE END AS has_menu,
                        ofm.first_message_at,
                        ofao.first_order_at,
                        -- hours to stage
                        CASE WHEN ofm.first_message_at IS NOT NULL
                             THEN EXTRACT(EPOCH FROM (ofm.first_message_at - o.created_at)) / 3600.0
                        END                                               AS hours_to_message,
                        CASE WHEN ofao.first_order_at IS NOT NULL
                             THEN EXTRACT(EPOCH FROM (ofao.first_order_at - o.created_at)) / 3600.0
                        END                                               AS hours_to_order
                    FROM organizations o
                    LEFT JOIN org_menu          om   ON om.org_id  = o.id
                    LEFT JOIN org_first_message ofm  ON ofm.org_id = o.id
                    LEFT JOIN org_first_any_order ofao ON ofao.org_id = o.id
                    ORDER BY o.created_at DESC
                    """
                )
        except Exception as exc:
            log.exception("analytics.activation.query_error", exc_type=type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={"detail": "Failed to fetch activation funnel data"}
            )

    # Build per-org list and aggregate counts
    signed_up = 0
    menu_loaded = 0
    first_message = 0
    first_order = 0

    hours_to_message_vals: list[float] = []
    hours_to_order_vals: list[float] = []

    per_org = []
    for row in rows:
        signed_up += 1
        has_menu = bool(row["has_menu"])
        has_msg = row["first_message_at"] is not None
        has_ord = row["first_order_at"] is not None

        if has_menu:
            menu_loaded += 1
        if has_msg:
            first_message += 1
        if has_ord:
            first_order += 1

        if row["hours_to_message"] is not None:
            hours_to_message_vals.append(float(row["hours_to_message"]))
        if row["hours_to_order"] is not None:
            hours_to_order_vals.append(float(row["hours_to_order"]))

        # Current stage: highest stage reached
        if has_ord:
            stage = 4
        elif has_msg:
            stage = 3
        elif has_menu:
            stage = 2
        else:
            stage = 1

        per_org.append({
            "org_id": row["org_id"],
            "org_name": row["org_name"] or f"Org #{row['org_id']}",
            "stage": stage,
            "signed_up_at": str(row["signed_up_at"].date()) if row["signed_up_at"] else None,
            "has_menu": has_menu,
            "first_message_at": str(row["first_message_at"].date()) if row["first_message_at"] else None,
            "first_order_at": str(row["first_order_at"].date()) if row["first_order_at"] else None,
        })

    def _percentile(vals: list[float], pct: float) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        idx = int(len(s) * pct / 100)
        idx = min(idx, len(s) - 1)
        return round(s[idx], 1)

    base = signed_up or 1  # avoid divide-by-zero

    funnel = {
        "signed_up": signed_up,
        "menu_loaded": menu_loaded,
        "first_message": first_message,
        "first_order": first_order,
    }
    funnel_pct = {
        "signed_up": 100.0,
        "menu_loaded": round(menu_loaded / base * 100, 1),
        "first_message": round(first_message / base * 100, 1),
        "first_order": round(first_order / base * 100, 1),
    }
    time_to_stage = {
        "first_message_median_h": _percentile(hours_to_message_vals, 50),
        "first_message_p75_h": _percentile(hours_to_message_vals, 75),
        "first_order_median_h": _percentile(hours_to_order_vals, 50),
        "first_order_p75_h": _percentile(hours_to_order_vals, 75),
    }

    return {
        "funnel": funnel,
        "funnel_pct": funnel_pct,
        "time_to_stage": time_to_stage,
        "per_org": per_org,
    }


# ── Churn risk ────────────────────────────────────────────────────────────────

@router.get("/api/internal/analytics/churn-risk")
async def analytics_churn_risk(_: None = Depends(verify_superadmin)):
    """Return tenants at churn risk: last-7d avg < 50% of 14d baseline."""
    pool = await get_pool()

    with bypass_tenant_scope("internal_churn_risk_cross_tenant"):
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH bot_orgs AS (
                        SELECT
                            l.org_id,
                            o.name AS org_name,
                            COALESCE(l.whatsapp_number, o.whatsapp_number) AS bot_number
                        FROM locations l
                        JOIN organizations o ON o.id = l.org_id
                        WHERE COALESCE(l.whatsapp_number, o.whatsapp_number) IS NOT NULL
                    ),
                    daily AS (
                        SELECT
                            bo.org_id,
                            bo.org_name,
                            (c.created_at::date)        AS day,
                            COUNT(*)                    AS cnt
                        FROM conversations c
                        JOIN bot_orgs bo ON bo.bot_number = c.bot_number
                        WHERE c.created_at >= CURRENT_DATE - INTERVAL '21 days'
                        GROUP BY bo.org_id, bo.org_name, c.created_at::date
                    ),
                    baseline AS (
                        SELECT org_id, org_name,
                            AVG(cnt)::float AS baseline_avg
                        FROM daily
                        WHERE day < CURRENT_DATE - INTERVAL '7 days'
                        GROUP BY org_id, org_name
                    ),
                    recent AS (
                        SELECT org_id,
                            AVG(cnt)::float AS recent_avg
                        FROM daily
                        WHERE day >= CURRENT_DATE - INTERVAL '7 days'
                        GROUP BY org_id
                    )
                    SELECT
                        b.org_id,
                        b.org_name,
                        ROUND(b.baseline_avg::numeric, 2) AS baseline_avg,
                        ROUND(COALESCE(r.recent_avg, 0)::numeric, 2) AS recent_avg,
                        ROUND(
                            (COALESCE(r.recent_avg, 0) / NULLIF(b.baseline_avg, 0))::numeric,
                            3
                        )                               AS ratio
                    FROM baseline b
                    LEFT JOIN recent r ON r.org_id = b.org_id
                    WHERE b.baseline_avg >= 3
                      AND COALESCE(r.recent_avg, 0) < 0.5 * b.baseline_avg
                    ORDER BY ratio ASC NULLS LAST
                    LIMIT 20
                    """
                )
        except Exception as exc:
            log.exception("analytics.churn_risk.query_error", exc_type=type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={"detail": "Failed to fetch churn risk data"}
            )

    at_risk = []
    for row in rows:
        baseline = float(row["baseline_avg"])
        recent = float(row["recent_avg"])
        ratio = float(row["ratio"]) if row["ratio"] is not None else 0.0
        drop_pct = round((1.0 - ratio) * 100, 1)
        at_risk.append({
            "org_id": row["org_id"],
            "org_name": row["org_name"] or f"Org #{row['org_id']}",
            "baseline_avg_daily": baseline,
            "recent_avg_daily": recent,
            "ratio": ratio,
            "drop_pct": drop_pct,
        })

    return {"at_risk": at_risk}


# ── North-star: Pedidos Rescatados ────────────────────────────────────────────

@router.get("/api/internal/analytics/pedidos-rescatados")
async def analytics_pedidos_rescatados(
    period: str = "mtd",
    _: None = Depends(verify_superadmin),
):
    """
    Cross-tenant north-star metric — Pedidos Rescatados.

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
    from app.repositories.north_star_repo import db_count_pedidos_rescatados_global

    today = date.today()
    if period == "mtd":
        period_start = today.replace(day=1)
        period_end   = today
    else:  # 30d default
        period_start = today - timedelta(days=29)
        period_end   = today

    with bypass_tenant_scope("internal_analytics_pedidos_rescatados_cross_tenant"):
        try:
            ranking = await db_count_pedidos_rescatados_global(period_start, period_end)
        except Exception as exc:
            log.exception("analytics.pedidos_rescatados_failed")
            return JSONResponse(status_code=500, content={"detail": "Error al calcular pedidos rescatados"})

    total = sum(r["count"] for r in ranking)
    return {
        "period": period,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total": total,
        "ranking": ranking,
    }
