"""
Platform-wide counts for Mesio HQ (home KPIs, Monitoring, Ctrl+K search).

Callers MUST be inside bypass_tenant_scope: every query goes through
tenant_connection(), which then runs as mesio_superadmin. The queries these
replace used a bare pool.acquire(); the pool connects as mesio_app, and on
the RLS tables (orders, table_orders, table_sessions, conversations…) an
unscoped mesio_app connection sees ZERO rows — HQ showed "0 pedidos hoy" and
"sin actividad" for restaurants that were selling.

"An order" is what a restaurant sells through Mesio: a table round
(table_orders) or a web delivery/pickup order (orders), cancelled ones out.
The live demo (/demo) recycles orders all day and is left out of every count.
"""
from __future__ import annotations

from datetime import datetime

from app.services.live_demo import DEMO_SLUG
from app.services.tenant_db import tenant_connection

_ROUND_CANCELLED = ("cancelled", "cancelado")
_WEB_CANCELLED = ("cancelado", "rechazado", "cancelled")

# Every sale as (org_id, created_at, total), demo excluded. $1 = demo slug,
# $2/$3 = round/web cancelled statuses.
_SALES_CTE = """
    WITH demo AS (SELECT id FROM organizations WHERE slug = $1),
    sales AS (
        SELECT t.org_id, t.created_at, t.total FROM table_orders t
         WHERE t.status <> ALL($2::text[])
           AND t.org_id NOT IN (SELECT id FROM demo)
        UNION ALL
        SELECT o.org_id, o.created_at, o.total FROM orders o
         WHERE o.status <> ALL($3::text[])
           AND o.org_id NOT IN (SELECT id FROM demo)
    )
"""


def _args() -> tuple:
    return (DEMO_SLUG, list(_ROUND_CANCELLED), list(_WEB_CANCELLED))


async def db_platform_overview(today_start_utc: datetime) -> dict:
    """Home KPIs. `today_start_utc` = start of Mesio's business day (Bogotá), naive UTC."""
    async with tenant_connection() as conn:
        sales = await conn.fetchrow(
            _SALES_CTE + """
            SELECT
              COUNT(*) FILTER (WHERE created_at >= $4)                         AS orders_today,
              COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS orders_7d,
              COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS orders_month,
              COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS orders_30d,
              COALESCE(SUM(total) FILTER (WHERE created_at >= $4), 0)                        AS sales_today,
              COALESCE(SUM(total) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'), 0) AS sales_7d,
              COUNT(DISTINCT org_id) FILTER (WHERE created_at >= $4)                         AS active_today,
              COUNT(DISTINCT org_id) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS active_7d,
              COUNT(DISTINCT org_id) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS active_30d
            FROM sales""",
            *_args(), today_start_utc,
        )
        orgs = await conn.fetchrow(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS new_7d,
                      COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS new_month,
                      (SELECT COUNT(*) FROM locations l
                        WHERE l.active AND l.org_id IN (SELECT id FROM organizations
                                                        WHERE COALESCE(slug, '') <> $1)) AS sedes
                 FROM organizations WHERE COALESCE(slug, '') <> $1""",
            DEMO_SLUG,
        )
        diners_today = await conn.fetchval(
            """SELECT COUNT(*) FROM diner_sessions d
                WHERE d.created_at >= ($2::timestamp AT TIME ZONE 'UTC')
                  AND d.org_id NOT IN (SELECT id FROM organizations WHERE slug = $1)""",
            DEMO_SLUG, today_start_utc,
        )
        errors_24h = await conn.fetchval(
            "SELECT COUNT(*) FROM platform_errors WHERE created_at >= NOW() - INTERVAL '24 hours'"
        )
        open_alerts = await conn.fetchrow(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE severity = 'critical') AS critical
                 FROM hq_alerts WHERE status = 'open'"""
        )
        invoices = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE created_at >= $1)                         AS today,
                      COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS month
                 FROM fiscal_invoices""",
            today_start_utc,
        )
    return {
        "orders": {
            "today": int(sales["orders_today"]), "this_week": int(sales["orders_7d"]),
            "this_month": int(sales["orders_month"]),
            "avg_daily_30d": round(int(sales["orders_30d"]) / 30, 1),
            "sales_today": sales["sales_today"], "sales_7d": sales["sales_7d"],
        },
        "restaurants": {
            "total": int(orgs["total"]), "sedes": int(orgs["sedes"] or 0),
            "active_today": int(sales["active_today"]), "active_7d": int(sales["active_7d"]),
            "active_30d": int(sales["active_30d"]),
            "new_this_week": int(orgs["new_7d"]), "new_this_month": int(orgs["new_month"]),
        },
        "diners_today": int(diners_today or 0),
        "errors_24h": int(errors_24h or 0),
        "alerts": {"open": int(open_alerts["total"]), "critical": int(open_alerts["critical"])},
        "billing": {"invoices_today": int(invoices["today"]), "invoices_this_month": int(invoices["month"])},
    }


async def db_ops_counts(today_start_utc: datetime) -> dict:
    """Live counts for Monitoring."""
    async with tenant_connection() as conn:
        orders_today = await conn.fetchval(
            _SALES_CTE + "SELECT COUNT(*) FROM sales WHERE created_at >= $4",
            *_args(), today_start_utc,
        )
        open_sittings = await conn.fetchval(
            "SELECT COUNT(*) FROM table_sessions WHERE closed_at IS NULL AND status = 'active'"
        )
        diners_30m = await conn.fetchval(
            "SELECT COUNT(*) FROM diner_sessions WHERE last_seen_at > NOW() - INTERVAL '30 minutes'"
        )
        orgs = await conn.fetchval("SELECT COUNT(*) FROM organizations WHERE COALESCE(slug, '') <> $1", DEMO_SLUG)
        errors_1h = await conn.fetchval(
            "SELECT COUNT(*) FROM platform_errors WHERE created_at >= NOW() - INTERVAL '1 hour'"
        )
    return {
        "orders_today": int(orders_today or 0),
        "active_table_sessions": int(open_sittings or 0),
        "active_diners": int(diners_30m or 0),
        "restaurants_total": int(orgs or 0),
        "errors_1h": int(errors_1h or 0),
    }


async def db_last_order_at(org_ids: list[int]) -> dict[int, datetime]:
    """Most recent table round or web order per org."""
    if not org_ids:
        return {}
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT org_id, MAX(created_at) AS last_at FROM (
                   SELECT org_id, created_at FROM table_orders WHERE org_id = ANY($1::int[])
                   UNION ALL
                   SELECT org_id, created_at FROM orders WHERE org_id = ANY($1::int[])
               ) x GROUP BY org_id""",
            org_ids,
        )
    return {int(r["org_id"]): r["last_at"] for r in rows if r["last_at"]}
