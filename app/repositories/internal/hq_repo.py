"""
Mesio HQ — the control-center snapshot of one organization and each of its
sedes: business, operation, adoption and staff.

Cross-tenant, read-only. Callers MUST be inside bypass_tenant_scope (the
connection then runs as mesio_superadmin, so FORCE RLS tables are readable).

Time windows are UTC instants computed by the caller: "today" is the sede's
own local day (locations.timezone), passed in as `today_start` (naive UTC,
the shape of the timestamp-without-time-zone columns).
"""
from __future__ import annotations

from datetime import datetime

from app.services.tenant_db import tenant_connection

# Rounds that still owe the kitchen something.
_KITCHEN_OPEN = ("recibido", "en_preparacion", "pendiente")
# Web delivery/pickup orders still in flight (delivery_repo vocabulary).
_DELIVERY_TERMINAL = ("rechazado", "cancelado", "entregado")
_CANCELLED = ("cancelled", "cancelado")


async def db_hq_org(org_id: int) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """SELECT id, name, slug, plan_code, subscription_status, founder_price_cop,
                      comp_until, paid_until, created_at, features
                 FROM organizations WHERE id = $1""",
            org_id,
        )
        if not row:
            return None
        users = await conn.fetch(
            """SELECT u.username, u.role, u.display_name, u.location_id, u.created_at,
                      (SELECT MAX(s.created_at) FROM sessions s WHERE s.username = u.username) AS last_login
                 FROM users u WHERE u.org_id = $1
                ORDER BY u.created_at""",
            org_id,
        )
        locations = await conn.fetch(
            """SELECT id, name, active, timezone, address, phone, opening_hours,
                      delivery_config, ops_config, created_at
                 FROM locations WHERE org_id = $1 ORDER BY id""",
            org_id,
        )
        usage = await conn.fetchrow(
            """SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0)::bigint AS tokens_30d,
                      MAX(usage_date) AS last_llm_day
                 FROM subscription_usage
                WHERE org_id = $1 AND usage_date >= CURRENT_DATE - 30""",
            org_id,
        )
    return {
        "org": dict(row),
        "users": [dict(u) for u in users],
        "locations": [dict(loc) for loc in locations],
        "usage": dict(usage) if usage else {},
    }


async def db_hq_location_metrics(org_id: int, location_id: int, today_start: datetime) -> dict:
    """Everything the HQ shows for one sede. Every query filters BOTH org_id
    and location_id: ids collide across tenants on purpose in tests."""
    async with tenant_connection() as conn:
        table = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE created_at >= $3)                          AS rounds_today,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')   AS rounds_7d,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days')  AS rounds_30d,
                 COALESCE(SUM(total) FILTER (WHERE created_at >= $3), 0)                         AS sales_today,
                 COALESCE(SUM(total) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'), 0)  AS sales_7d,
                 COALESCE(SUM(total) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS sales_30d,
                 COUNT(DISTINCT COALESCE(base_order_id, id)) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS bills_30d,
                 COUNT(*) FILTER (WHERE status = ANY($4::text[]) AND created_at < NOW() - INTERVAL '45 minutes'
                                  AND created_at >= NOW() - INTERVAL '2 days')     AS stuck_rounds,
                 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM ready_at - created_at) / 60)
                   FILTER (WHERE ready_at IS NOT NULL AND created_at >= NOW() - INTERVAL '7 days') AS kitchen_p50_min,
                 PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM ready_at - created_at) / 60)
                   FILTER (WHERE ready_at IS NOT NULL AND created_at >= NOW() - INTERVAL '7 days') AS kitchen_p90_min,
                 COUNT(*) FILTER (WHERE ready_at IS NOT NULL AND created_at >= NOW() - INTERVAL '7 days') AS kitchen_samples,
                 MAX(created_at)                                                    AS last_round_at
               FROM table_orders
              WHERE org_id = $1 AND location_id = $2 AND status <> ALL($5::text[])""",
            org_id, location_id, today_start, list(_KITCHEN_OPEN), list(_CANCELLED),
        )
        web = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE created_at >= $3)                          AS orders_today,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')   AS orders_7d,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days')  AS orders_30d,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days' AND order_type ILIKE 'domicilio%') AS delivery_30d,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days' AND order_type NOT ILIKE 'domicilio%') AS pickup_30d,
                 COALESCE(SUM(total) FILTER (WHERE paid AND created_at >= $3), 0)                         AS sales_today,
                 COALESCE(SUM(total) FILTER (WHERE paid AND created_at >= NOW() - INTERVAL '7 days'), 0)  AS sales_7d,
                 COALESCE(SUM(total) FILTER (WHERE paid AND created_at >= NOW() - INTERVAL '30 days'), 0) AS sales_30d,
                 COUNT(*) FILTER (WHERE status <> ALL($4::text[]) AND created_at < NOW() - INTERVAL '90 minutes'
                                  AND created_at >= NOW() - INTERVAL '2 days')     AS stuck_orders,
                 COUNT(*) FILTER (WHERE status = 'pendiente_aceptacion')           AS waiting_acceptance,
                 COUNT(*) FILTER (WHERE status = 'entregado' AND NOT paid AND created_at >= NOW() - INTERVAL '30 days') AS delivered_unpaid,
                 MAX(created_at)                                                    AS last_order_at
               FROM orders
              WHERE org_id = $1 AND location_id = $2""",
            org_id, location_id, today_start, list(_DELIVERY_TERMINAL),
        )
        floor = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM restaurant_tables
                   WHERE org_id = $1 AND location_id = $2 AND active)                  AS tables,
                 (SELECT COUNT(DISTINCT table_id) FROM table_sessions
                   WHERE org_id = $1 AND location_id = $2 AND status = 'active')       AS tables_open,
                 (SELECT COUNT(*) FROM table_sessions
                   WHERE org_id = $1 AND location_id = $2 AND status = 'active'
                     AND started_at < NOW() - INTERVAL '6 hours')                      AS sittings_over_6h,
                 (SELECT COUNT(*) FROM waiter_alerts
                   WHERE org_id = $1 AND location_id = $2 AND NOT dismissed
                     AND created_at >= NOW() - INTERVAL '1 day')                      AS open_waiter_alerts""",
            org_id, location_id,
        )
        checks = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE tc.status = 'open' AND tc.created_at < NOW() - INTERVAL '3 hours') AS checks_open_over_3h,
                 COUNT(*) FILTER (WHERE tc.proposal_status = 'proof_received')                          AS proofs_to_review
               FROM table_checks tc
              WHERE EXISTS (SELECT 1 FROM table_orders t
                             WHERE COALESCE(t.base_order_id, t.id) = tc.base_order_id
                               AND t.org_id = $1 AND t.location_id = $2)""",
            org_id, location_id,
        )
        diners = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')                           AS sessions_7d,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days' AND order_mode = 'dine_in') AS table_sessions_7d,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days' AND customer_profile_id IS NOT NULL) AS remembered_7d,
                 MAX(last_seen_at)                                                                          AS last_diner_at
               FROM diner_sessions
              WHERE org_id = $1 AND location_id = $2""",
            org_id, location_id,
        )
        chat = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE updated_at >= NOW() - INTERVAL '7 days') AS conversations_7d
                 FROM conversations WHERE org_id = $1 AND location_id = $2""",
            org_id, location_id,
        )
        nps = await conn.fetchrow(
            """SELECT COUNT(*)                                   AS responses,
                      COUNT(*) FILTER (WHERE score >= 9)         AS promoters,
                      COUNT(*) FILTER (WHERE score <= 6)         AS detractors,
                      COUNT(*) FILTER (WHERE score <= 6 AND COALESCE(comment, '') <> '') AS detractor_comments
                 FROM nps_responses
                WHERE org_id = $1 AND location_id = $2 AND created_at >= NOW() - INTERVAL '30 days'""",
            org_id, location_id,
        )
        modules = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM reservations
                   WHERE org_id = $1 AND location_id = $2 AND created_at >= NOW() - INTERVAL '30 days') AS reservations_30d,
                 (SELECT COUNT(*) FROM inventory WHERE org_id = $1 AND location_id = $2)               AS inventory_items,
                 (SELECT COUNT(*) FROM inventory WHERE org_id = $1 AND location_id = $2
                     AND current_stock <= min_stock)                                                  AS inventory_low""",
            org_id, location_id,
        )
        staff = await conn.fetch(
            """SELECT st.id, st.name, st.role, st.roles, st.active, st.username,
                      (SELECT MAX(s.created_at) FROM sessions s WHERE s.username = 'staff:' || st.id::text) AS last_login
                 FROM staff st
                WHERE st.org_id = $1 AND st.location_id = $2
                ORDER BY st.active DESC, st.name""",
            org_id, location_id,
        )
    return {
        "table": dict(table), "web": dict(web), "floor": dict(floor), "checks": dict(checks),
        "diners": dict(diners), "chat": dict(chat), "nps": dict(nps), "modules": dict(modules),
        "staff": [dict(s) for s in staff],
    }
