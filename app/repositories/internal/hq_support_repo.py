"""
Mesio HQ support actions — the closed set of fixes Mesio may apply to a
restaurant (PM 2026-10-02): close a stuck sitting, cancel a hung order,
dismiss old waiter alerts, clear sold-outs, plus the lists the ficha shows
so you can pick what to fix.

Every statement filters org_id (and location_id where it applies): an id
from another restaurant matches nothing. Callers MUST be inside
bypass_tenant_scope (Mesio acts across restaurants) and must write the
hq_audit_log entry with the reason.

Money is never touched: no amounts, no "paid" flags.
"""
from __future__ import annotations

from app.services.tenant_db import tenant_connection

_OPEN_ROUND = ("recibido", "en_preparacion", "pendiente")
_WEB_TERMINAL = ("rechazado", "cancelado", "entregado")


async def db_location_of_org(org_id: int, location_id: int) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, ops_config FROM locations WHERE id = $1 AND org_id = $2",
            location_id, org_id,
        )
    return dict(row) if row else None


async def db_support_lists(org_id: int, location_id: int) -> dict:
    """What can be fixed at one sede right now."""
    async with tenant_connection() as conn:
        sittings = await conn.fetch(
            """SELECT s.id, s.table_id, s.table_name, s.started_at, s.last_activity,
                      (SELECT COALESCE(SUM(t.total), 0) FROM table_orders t
                        WHERE t.org_id = s.org_id AND t.table_id = s.table_id
                          AND t.created_at >= s.started_at
                          AND t.status NOT IN ('cancelled', 'cancelado')) AS rounds_total
                 FROM table_sessions s
                WHERE s.org_id = $1 AND s.location_id = $2 AND s.status = 'active'
                ORDER BY s.started_at""",
            org_id, location_id,
        )
        rounds = await conn.fetch(
            """SELECT id, table_name, status, total, created_at, station
                 FROM table_orders
                WHERE org_id = $1 AND location_id = $2 AND status = ANY($3::text[])
                  AND created_at < NOW() - INTERVAL '45 minutes'
                ORDER BY created_at LIMIT 50""",
            org_id, location_id, list(_OPEN_ROUND),
        )
        web = await conn.fetch(
            """SELECT id, public_code, order_type, status, total, created_at, customer_name
                 FROM orders
                WHERE org_id = $1 AND location_id = $2 AND status <> ALL($3::text[])
                  AND created_at < NOW() - INTERVAL '90 minutes'
                ORDER BY created_at LIMIT 50""",
            org_id, location_id, list(_WEB_TERMINAL),
        )
        alerts = await conn.fetchrow(
            """SELECT COUNT(*) AS open, MIN(created_at) AS oldest FROM waiter_alerts
                WHERE org_id = $1 AND location_id = $2 AND NOT dismissed""",
            org_id, location_id,
        )
        sold_out = await conn.fetch(
            """SELECT dish_name, updated_at FROM menu_availability
                WHERE org_id = $1 AND location_id = $2 AND NOT available
                ORDER BY dish_name""",
            org_id, location_id,
        )
        dian = await conn.fetch(
            """SELECT id, order_id, prefix, invoice_number, total_cents, dian_status, created_at
                 FROM fiscal_invoices
                WHERE org_id = $1 AND (location_id = $2 OR location_id IS NULL)
                  AND dian_status NOT IN ('accepted')
                  AND created_at < NOW() - INTERVAL '10 minutes'
                ORDER BY created_at DESC LIMIT 20""",
            org_id, location_id,
        )
    return {
        "sittings": [dict(r) for r in sittings],
        "stuck_rounds": [dict(r) for r in rounds],
        "stuck_web_orders": [dict(r) for r in web],
        "waiter_alerts": dict(alerts) if alerts else {"open": 0, "oldest": None},
        "sold_out": [dict(r) for r in sold_out],
        "dian_pending": [dict(r) for r in dian],
    }


async def db_close_sitting(org_id: int, session_id: int) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE table_sessions
                  SET status = 'closed', closed_at = NOW(), closed_by = 'mesio_support',
                      closed_by_username = 'Soporte Mesio',
                      summary = jsonb_build_object('close_reason', 'mesio_support')
                WHERE id = $1 AND org_id = $2 AND status IN ('active', 'nps_pending')
            RETURNING id, table_id, table_name, location_id""",
            session_id, org_id,
        )
    return dict(row) if row else None


async def db_cancel_round(org_id: int, order_id: str) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE table_orders SET status = 'cancelled', updated_at = NOW()
                WHERE id = $1 AND org_id = $2 AND status = ANY($3::text[])
            RETURNING id, table_id, table_name, location_id, total, status""",
            order_id, org_id, list(_OPEN_ROUND),
        )
    return dict(row) if row else None


async def db_cancel_web_order(org_id: int, order_id: str, reason: str) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE orders
                  SET status = 'cancelado', cancelled_at = NOW(), cancelled_reason = $3
                WHERE id = $1 AND org_id = $2 AND status <> ALL($4::text[])
            RETURNING id, public_code, location_id, total, status""",
            order_id, org_id, reason, list(_WEB_TERMINAL),
        )
    return dict(row) if row else None


async def db_dismiss_alerts(org_id: int, location_id: int, older_than_minutes: int) -> int:
    async with tenant_connection() as conn:
        result = await conn.execute(
            """UPDATE waiter_alerts SET dismissed = TRUE
                WHERE org_id = $1 AND location_id = $2 AND NOT dismissed
                  AND created_at < NOW() - make_interval(mins => $3)""",
            org_id, location_id, older_than_minutes,
        )
    return int(result.split()[-1])


async def db_clear_sold_out(org_id: int, location_id: int, dish_name: str | None) -> int:
    async with tenant_connection() as conn:
        result = await conn.execute(
            """UPDATE menu_availability SET available = TRUE, updated_at = NOW()
                WHERE org_id = $1 AND location_id = $2 AND NOT available
                  AND ($3::text IS NULL OR dish_name = $3)""",
            org_id, location_id, dish_name,
        )
    return int(result.split()[-1])


async def db_user_of_org(org_id: int, username: str) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT username, role, restaurant_name FROM users WHERE username = $1 AND org_id = $2",
            username.lower().strip(), org_id,
        )
    return dict(row) if row else None


async def db_staff_of_org(org_id: int, staff_id: str) -> dict | None:
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, location_id FROM staff WHERE id::text = $1 AND org_id = $2",
            staff_id, org_id,
        )
    return dict(row) if row else None
