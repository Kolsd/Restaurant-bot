"""
app/repositories/north_star_repo.py

North-star metric — "Pedidos Rescatados" (bot-originated orders).

Definition (CEO-confirmed 2026-05-07):
  A "pedido rescatado" = any order created via the bot — WhatsApp
  (channel = 'whatsapp_bot') OR Mesio's own diner web-chat
  (channel = 'web_chat', added 2026-09 when WhatsApp stopped being the only
  bot surface) — regardless of status, including cancelled. It represents
  demand the restaurant captured via Mesio, independent of which channel
  the diner happened to use.

Sources:
  - orders        (delivery / pickup external orders)  channel IN _CHANNELS
  - table_orders  (in-restaurant mesa orders)          channel IN _CHANNELS

Functions:
  db_count_pedidos_rescatados       — single-tenant, requires active tenant_scope
  db_count_pedidos_rescatados_global — cross-tenant ranking, requires bypass_tenant_scope
"""
from __future__ import annotations

from datetime import date

from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)

# Every bot-originated channel counts as a "pedido rescatado" — the metric is
# about demand captured BY THE BOT, not by any one messaging surface.
_CHANNELS = ("whatsapp_bot", "web_chat")


async def db_count_pedidos_rescatados(
    period_start: date,
    period_end: date,
) -> dict:
    """
    Count bot-originated orders for the current tenant in [period_start, period_end].

    Returns: {"count": int, "delivery": int, "table": int}

    Tenant-scoped: requires active tenant_scope.
    Both periods are inclusive (>= period_start AND <= period_end).
    Cancelled orders are counted (CEO decision: total demand captured).
    """
    async with tenant_connection() as conn:
        delivery = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE channel = ANY($1)
              AND created_at::date >= $2
              AND created_at::date <= $3
            """,
            list(_CHANNELS), period_start, period_end,
        )
        table = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM table_orders
            WHERE channel = ANY($1)
              AND created_at::date >= $2
              AND created_at::date <= $3
            """,
            list(_CHANNELS), period_start, period_end,
        )

    delivery = int(delivery or 0)
    table = int(table or 0)
    return {
        "count": delivery + table,
        "delivery": delivery,
        "table": table,
    }


async def db_count_pedidos_rescatados_global(
    period_start: date,
    period_end: date,
) -> list[dict]:
    """
    Cross-tenant aggregation for /internal/analytics.

    Returns a list sorted descending by total count:
      [{"org_id": int, "org_name": str, "count": int, "delivery": int, "table": int}, ...]

    MUST be called under bypass_tenant_scope. Uses tenant_connection() (never
    pool.acquire() directly) so the bypass actually executes
    `SET LOCAL ROLE mesio_superadmin` — orders/table_orders are RLS tables
    (migration 0029); without that role switch a pooled connection would read
    under whatever org_id scope it last had set, not a real cross-tenant view.
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT
                o.org_id,
                org.name                                AS org_name,
                COUNT(o.id)                             AS delivery_count,
                0::bigint                               AS table_count
            FROM orders o
            JOIN organizations org ON org.id = o.org_id
            WHERE o.channel = ANY($1)
              AND o.created_at::date >= $2
              AND o.created_at::date <= $3
            GROUP BY o.org_id, org.name

            UNION ALL

            SELECT
                to2.org_id,
                org.name,
                0::bigint,
                COUNT(to2.id)
            FROM table_orders to2
            JOIN organizations org ON org.id = to2.org_id
            WHERE to2.channel = ANY($1)
              AND to2.created_at::date >= $2
              AND to2.created_at::date <= $3
            GROUP BY to2.org_id, org.name
            """,
            list(_CHANNELS), period_start, period_end,
        )

    # Aggregate UNION results per org_id
    agg: dict[int, dict] = {}
    for row in rows:
        oid = row["org_id"]
        if oid not in agg:
            agg[oid] = {
                "org_id": oid,
                "org_name": row["org_name"],
                "delivery": 0,
                "table": 0,
            }
        agg[oid]["delivery"] += int(row["delivery_count"] or 0)
        agg[oid]["table"]    += int(row["table_count"] or 0)

    result = []
    for item in agg.values():
        item["count"] = item["delivery"] + item["table"]
        result.append(item)

    return sorted(result, key=lambda x: x["count"], reverse=True)
