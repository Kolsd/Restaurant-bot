"""
Stats repository — Dashboard Analytics Endpoints (Tier 2).

Provides aggregation queries for four dashboard endpoints:
  - by_channel     : Sales breakdown by channel (orders + table_orders)
  - top_dishes     : Top N dishes by revenue with food cost/margin
  - inventory_critical : Low-stock ingredients + dishes they affect
  - live_orders    : Unified active order feed (delivery + table)

All functions require an active tenant_scope() at the call site.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from app.services.logging import get_logger
from app.services.money import to_decimal, quantize_money

log = get_logger(__name__)


def _to_date(s: str) -> date:
    """Parse YYYY-MM-DD string to datetime.date (required by asyncpg TIMESTAMPTZ params)."""
    return date.fromisoformat(s)


def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


def _default_period(period_start: str | None, period_end: str | None) -> tuple[str, str]:
    """Return (period_start, period_end). Default = last 7 days if absent."""
    if not period_start or not period_end:
        today = date.today()
        return str(today - timedelta(days=6)), str(today)
    return period_start, period_end


def _prev_period(period_start: str, period_end: str) -> tuple[str, str]:
    """Return the previous period of equal duration immediately before period_start.

    Example: period 2026-04-11 → 2026-04-17 (7 days)
             previous → 2026-04-04 → 2026-04-10 (7 days, same duration)
    """
    ps = date.fromisoformat(period_start)
    pe = date.fromisoformat(period_end)
    duration = pe - ps  # timedelta
    prev_end = ps - timedelta(days=1)
    prev_start = prev_end - duration
    return str(prev_start), str(prev_end)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Sales by channel
# ─────────────────────────────────────────────────────────────────────────────

_CHANNEL_LABELS: dict[str, str] = {
    "whatsapp_bot": "WhatsApp · Bot",
    "pos":          "Salón · POS",
    "qr_table":     "QR de mesa",
    "delivery":     "Domicilios",
    "web":          "Web",
    "unknown":      "Sin clasificar",
}


def _classify_channel(channel: str | None, order_type: str | None) -> str:
    """Map raw channel + order_type to one of our canonical buckets."""
    ch = (channel or "").strip().lower()
    ot = (order_type or "").strip().lower()

    if ch == "whatsapp_bot":
        return "whatsapp_bot"
    if ch in ("pos", "salon"):
        return "pos"
    if ch == "qr_table":
        return "qr_table"
    # Delivery orders without explicit channel → delivery bucket
    if ot in ("domicilio",):
        return "delivery"
    if ch in ("web",):
        return "web"
    if ch:
        # Known but unmapped channel — keep as-is for forward compat
        return ch
    return "unknown"


async def db_sales_by_channel(
    org_id: int,
    period_start: str,
    period_end: str,
    location_id: int | None = None,
) -> dict:
    """
    Aggregate sales by channel for the given org and period.

    Combines `orders` (delivery/pickup) and `table_orders` (salon) into one
    channel breakdown.  Both tables are RLS-protected via org_id; the caller
    must be inside tenant_scope(org_id).
    """
    from datetime import timedelta as _td  # noqa: PLC0415

    ps = _to_date(period_start)
    # Make end date inclusive by querying < (end + 1 day)
    d_to_inclusive = _to_date(period_end) + _td(days=1)

    async with _tenant_connection() as conn:
        # ── delivery/pickup orders ──────────────────────────────────────────
        if location_id is not None:
            order_rows = await conn.fetch(
                """SELECT channel, order_type, total
                   FROM orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND paid = TRUE
                     AND location_id = $3""",
                ps, d_to_inclusive, location_id,
            )
        else:
            order_rows = await conn.fetch(
                """SELECT channel, order_type, total
                   FROM orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND paid = TRUE""",
                ps, d_to_inclusive,
            )

        # ── salon table orders ──────────────────────────────────────────────
        if location_id is not None:
            table_rows = await conn.fetch(
                """SELECT channel, total
                   FROM table_orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND status NOT IN ('cancelado')
                     AND branch_id = $3""",
                ps, d_to_inclusive, location_id,
            )
        else:
            table_rows = await conn.fetch(
                """SELECT channel, total
                   FROM table_orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND status NOT IN ('cancelado')""",
                ps, d_to_inclusive,
            )

    # ── aggregate ──────────────────────────────────────────────────────────
    buckets: dict[str, dict] = {}

    for row in order_rows:
        bucket = _classify_channel(row["channel"], row["order_type"])
        b = buckets.setdefault(bucket, {"total": Decimal("0"), "count": 0})
        b["total"] += to_decimal(row["total"])
        b["count"] += 1

    for row in table_rows:
        # table_orders are always salon — use channel if set, else default pos
        ch = (row["channel"] or "").strip().lower()
        bucket = ch if ch in ("pos", "qr_table") else ("pos" if not ch else ch)
        b = buckets.setdefault(bucket, {"total": Decimal("0"), "count": 0})
        b["total"] += to_decimal(row["total"])
        b["count"] += 1

    grand_total = sum(b["total"] for b in buckets.values())
    grand_count = sum(b["count"] for b in buckets.values())

    channels = []
    for key, b in sorted(buckets.items(), key=lambda x: -x[1]["total"]):
        pct = round(float(b["total"] / grand_total * 100), 1) if grand_total else 0.0
        channels.append({
            "channel": key,
            "label":   _CHANNEL_LABELS.get(key, key.replace("_", " ").title()),
            "total":   int(quantize_money(b["total"])),  # JSON boundary
            "count":   b["count"],
            "pct":     pct,
        })

    return {
        "period": {"start": period_start, "end": period_end},
        "total":       int(quantize_money(grand_total)),  # JSON boundary
        "total_count": grand_count,
        "channels":    channels,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Top dishes
# ─────────────────────────────────────────────────────────────────────────────

async def db_top_dishes(
    org_id: int,
    period_start: str,
    period_end: str,
    limit: int = 10,
    location_id: int | None = None,
) -> dict:
    """
    Returns top `limit` dishes by revenue with food cost and margin %.

    Sources:
      - orders.items JSONB (delivery/pickup) — RLS via org_id
      - table_orders.items JSONB (salon) — RLS via org_id
      - dish_recipes JOIN inventory for food_cost_per_unit

    Menu metadata (price, category) is read from organizations.menu.
    """
    from datetime import timedelta as _td  # noqa: PLC0415

    limit = max(1, min(50, limit))
    ps = _to_date(period_start)
    d_to_inclusive = _to_date(period_end) + _td(days=1)

    async with _tenant_connection() as conn:
        # ── collect all items from orders ───────────────────────────────────
        if location_id is not None:
            order_items_rows = await conn.fetch(
                """SELECT items
                   FROM orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND paid = TRUE
                     AND location_id = $3""",
                ps, d_to_inclusive, location_id,
            )
            table_items_rows = await conn.fetch(
                """SELECT items
                   FROM table_orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND status NOT IN ('cancelado')
                     AND branch_id = $3""",
                ps, d_to_inclusive, location_id,
            )
        else:
            order_items_rows = await conn.fetch(
                """SELECT items
                   FROM orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND paid = TRUE""",
                ps, d_to_inclusive,
            )
            table_items_rows = await conn.fetch(
                """SELECT items
                   FROM table_orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND status NOT IN ('cancelado')""",
                ps, d_to_inclusive,
            )

        # ── food costs from dish_recipes ────────────────────────────────────
        food_cost_rows = await conn.fetch(
            """SELECT r.dish_name,
                      ROUND(SUM(r.quantity * i.cost_per_unit)::numeric, 2) AS food_cost
               FROM dish_recipes r
               JOIN inventory i ON i.id = r.ingredient_id
               WHERE r.org_id = $1
               GROUP BY r.dish_name""",
            org_id,
        )

        # ── menu metadata ───────────────────────────────────────────────────
        menu_row = await conn.fetchval(
            "SELECT menu FROM organizations WHERE id = $1", org_id
        )

    # ── parse menu for price + category ────────────────────────────────────
    menu_meta: dict[str, dict] = {}  # dish_name → {price, category}
    if menu_row:
        try:
            raw = menu_row if isinstance(menu_row, dict) else json.loads(menu_row)
            for category, dishes in raw.items():
                if not isinstance(dishes, list):
                    continue
                for dish in dishes:
                    if isinstance(dish, dict) and dish.get("name"):
                        menu_meta[dish["name"]] = {
                            "price":    to_decimal(dish.get("price", 0)),
                            "category": category,
                        }
        except Exception:
            log.warning("stats_repo.top_dishes.menu_parse_error", org_id=org_id)

    # ── food cost lookup ────────────────────────────────────────────────────
    food_costs: dict[str, Decimal] = {
        r["dish_name"]: to_decimal(r["food_cost"])
        for r in food_cost_rows
    }

    # ── aggregate dish counts across all item lists ─────────────────────────
    dish_counts: dict[str, dict] = {}  # name → {sold, revenue}

    def _parse_items(raw) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return []
        return []

    for row in list(order_items_rows) + list(table_items_rows):
        for item in _parse_items(row["items"]):
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            if not name:
                continue
            qty = int(to_decimal(item.get("quantity", item.get("qty", 1))))
            price_unit = to_decimal(item.get("price", 0))
            revenue = price_unit * qty
            dc = dish_counts.setdefault(name, {"sold": 0, "revenue": Decimal("0")})
            dc["sold"] += qty
            dc["revenue"] += revenue

    # ── sort by revenue DESC, take top N ────────────────────────────────────
    ranked = sorted(dish_counts.items(), key=lambda x: -x[1]["revenue"])[:limit]

    dishes = []
    for rank, (name, counts) in enumerate(ranked, start=1):
        meta = menu_meta.get(name, {})
        price = meta.get("price", Decimal("0"))
        fc = food_costs.get(name)

        margin_pct = None
        if fc is not None and price > 0:
            margin_pct = round(float((price - fc) / price * 100), 1)

        dishes.append({
            "rank":               rank,
            "name":               name,
            "category":           meta.get("category"),
            "price":              int(quantize_money(price)),             # JSON boundary
            "sold":               counts["sold"],
            "revenue":            int(quantize_money(counts["revenue"])), # JSON boundary
            "food_cost_per_unit": int(quantize_money(fc)) if fc is not None else None,  # JSON boundary
            "margin_pct":         margin_pct,
        })

    return {
        "period": {"start": period_start, "end": period_end},
        "dishes": dishes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Inventory critical
# ─────────────────────────────────────────────────────────────────────────────

async def db_inventory_critical(
    org_id: int,
    ok_limit: int = 3,
) -> dict:
    """
    Returns low-stock ingredients sorted by severity, plus a few OK items.

    Severity:
      critical  — current_stock < min_stock * 0.5
      warning   — current_stock < min_stock
      ok        — current_stock >= min_stock

    Each alert includes `affects_dishes`: list of dish names that use this
    ingredient via dish_recipes.
    """
    async with _tenant_connection() as conn:
        inv_rows = await conn.fetch(
            """SELECT i.id, i.name, i.unit,
                      i.current_stock, i.min_stock
               FROM inventory i
               WHERE i.org_id = $1
               ORDER BY i.name""",
            org_id,
        )

        # ── reverse ingredient→dish mapping ────────────────────────────────
        recipe_rows = await conn.fetch(
            """SELECT ingredient_id, dish_name
               FROM dish_recipes
               WHERE org_id = $1""",
            org_id,
        )

    # Build reverse map: ingredient_id → [dish_name, …]
    ingredient_dishes: dict[int, list[str]] = {}
    for r in recipe_rows:
        ingredient_dishes.setdefault(r["ingredient_id"], []).append(r["dish_name"])

    alerts = []
    ok_items = []

    for row in inv_rows:
        current = float(row["current_stock"] or 0)
        minimum = float(row["min_stock"] or 0)

        if minimum > 0 and current < minimum * 0.5:
            severity = "critical"
        elif minimum > 0 and current < minimum:
            severity = "warning"
        else:
            severity = "ok"

        item = {
            "ingredient":    row["name"],
            "unit":          row["unit"],
            "current_stock": current,
            "min_stock":     minimum,
            "severity":      severity,
            "affects_dishes": ingredient_dishes.get(row["id"], []),
        }

        if severity in ("critical", "warning"):
            alerts.append(item)
        else:
            ok_items.append(item)

    # Sort alerts: critical first, then warning, then alphabetical
    severity_order = {"critical": 0, "warning": 1, "ok": 2}
    alerts.sort(key=lambda x: (severity_order[x["severity"]], x["ingredient"]))
    ok_items.sort(key=lambda x: x["ingredient"])

    return {
        "alerts": alerts,
        "ok":     ok_items[:ok_limit],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Live orders
# ─────────────────────────────────────────────────────────────────────────────

async def db_live_orders(
    org_id: int,
    limit: int = 20,
) -> dict:
    """
    Returns a unified live feed of active delivery + table orders.

    Delivery/pickup orders: status NOT IN ('entregado', 'cancelado')
    Table orders: status NOT IN ('cancelado', 'factura_entregada')

    Merges both lists, sorts by created_at DESC, caps at `limit`.
    """
    limit = max(1, min(100, limit))

    async with _tenant_connection() as conn:
        # ── delivery + pickup orders ────────────────────────────────────────
        delivery_rows = await conn.fetch(
            """SELECT id, phone, items, order_type, address, status,
                      total, created_at, channel, notes
               FROM orders
               WHERE status NOT IN ('entregado', 'cancelado')
               ORDER BY created_at DESC
               LIMIT $1""",
            limit,
        )

        # ── salon table orders ──────────────────────────────────────────────
        table_rows = await conn.fetch(
            """SELECT to2.id, to2.table_id, to2.table_name, to2.phone,
                      to2.items, to2.status, to2.total, to2.created_at,
                      to2.channel, to2.notes,
                      rt.capacity AS table_capacity
               FROM table_orders to2
               LEFT JOIN restaurant_tables rt ON rt.id = to2.table_id
               WHERE to2.status NOT IN ('cancelado', 'factura_entregada')
               ORDER BY to2.created_at DESC
               LIMIT $1""",
            limit,
        )

    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415

    now = _dt.now(_tz.utc)

    def _age_min(created_at) -> int:
        """Minutes since order was created."""
        if created_at is None:
            return 0
        if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=_tz.utc)
        delta = now - created_at
        return max(0, int(delta.total_seconds() // 60))

    def _items_summary(raw_items) -> str:
        items = raw_items
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                return str(items)
        if not isinstance(items, list):
            return ""
        names = []
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                names.append(it["name"])
        return " · ".join(names[:4])

    unified = []

    for row in delivery_rows:
        ot = row["order_type"] or "domicilio"
        source = "pickup" if ot == "recoger" else "delivery"
        phone = row["phone"] or ""
        unified.append({
            "id":                 f"#{row['id'][:6].upper()}" if len(row["id"]) >= 6 else f"#{row['id']}",
            "source":             source,
            "customer":           phone[-4:] and f"···{phone[-4:]}",
            "customer_phone_last4": phone[-4:] if len(phone) >= 4 else phone,
            "channel":            row["channel"] or "unknown",
            "status":             row["status"],
            "total":              int(quantize_money(to_decimal(row["total"]))),  # JSON boundary
            "age_min":            _age_min(row["created_at"]),
            "items_summary":      _items_summary(row["items"]),
            "address_short":      (row["address"] or "")[:50] or None,
            "_created_at":        row["created_at"],
        })

    for row in table_rows:
        unified.append({
            "id":           f"#{row['id'][:6].upper()}" if len(row["id"]) >= 6 else f"#{row['id']}",
            "source":       "table",
            "table_name":   row["table_name"],
            "party_size":   row["table_capacity"],
            "channel":      row["channel"] or "pos",
            "status":       row["status"],
            "total":        int(quantize_money(to_decimal(row["total"]))),  # JSON boundary
            "age_min":      _age_min(row["created_at"]),
            "items_summary": _items_summary(row["items"]),
            "_created_at":  row["created_at"],
        })

    # Sort merged list by created_at DESC, take top `limit`
    def _sort_key(o):
        ca = o.get("_created_at")
        if ca is None:
            return 0
        if hasattr(ca, "timestamp"):
            if hasattr(ca, "tzinfo") and ca.tzinfo is None:
                from datetime import timezone as _tz2  # noqa: PLC0415
                ca = ca.replace(tzinfo=_tz2.utc)
            return -ca.timestamp()
        return 0

    unified.sort(key=_sort_key)
    unified = unified[:limit]

    # Strip internal sort key before returning
    for o in unified:
        o.pop("_created_at", None)

    return {"orders": unified}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Payment status donut
# ─────────────────────────────────────────────────────────────────────────────

# Status bucket mapping:
#   paid      — orders.paid = TRUE, or table_checks.status = 'invoiced'
#   pending   — orders.paid = FALSE and status NOT cancelled/disputed,
#               or table_checks.status = 'open' / 'pending'
#   disputed  — orders.status LIKE 'dispute%' / 'reclamacion', or
#               orders.paid = FALSE and status = 'cancelado'
#   courtesy  — orders.total = 0 or status = 'cortesia'/'courtesy'
# Anything unrecognised falls into 'pending'.

def _payment_bucket(paid: bool, status: str, total) -> str:
    """Map (paid, status, total) to one of: paid / pending / disputed / courtesy."""
    s = (status or "").strip().lower()
    t = float(total or 0)

    # Courtesy first: zero-value or explicitly tagged
    if t == 0 or s in ("cortesia", "courtesy"):
        return "courtesy"
    # Disputed
    if s.startswith("dispute") or s in ("reclamacion", "reclamado"):
        return "disputed"
    # Explicit cancelled-unpaid = disputed
    if not paid and s == "cancelado":
        return "disputed"
    # Paid
    if paid:
        return "paid"
    # Everything else is pending
    return "pending"


_PAYMENT_LABELS = {
    "paid":     "Pagados",
    "pending":  "Pendientes",
    "disputed": "Disputa",
    "courtesy": "Cortesía",
}


async def db_payment_status(
    org_id: int,
    period_start: str,
    period_end: str,
    location_id: int | None = None,
) -> dict:
    """Aggregate payment status buckets for the donut chart.

    Sources:
      - orders (delivery/pickup): columns paid BOOL + status TEXT + total
      - table_checks: status 'invoiced'=paid, 'open'/'pending'=pending

    Bucket rules (see _payment_bucket docstring above).
    Both tables are RLS-protected; caller must be in tenant_scope(org_id).
    """
    from datetime import timedelta as _td  # noqa: PLC0415

    ps = _to_date(period_start)
    d_to_inclusive = _to_date(period_end) + _td(days=1)

    async with _tenant_connection() as conn:
        if location_id is not None:
            order_rows = await conn.fetch(
                """SELECT paid, status, total
                   FROM orders
                   WHERE created_at >= $1 AND created_at < $2
                     AND location_id = $3""",
                ps, d_to_inclusive, location_id,
            )
            check_rows = await conn.fetch(
                """SELECT tc.status, tc.total
                   FROM table_checks tc
                   JOIN table_orders to2 ON to2.id = tc.base_order_id
                   WHERE tc.created_at >= $1 AND tc.created_at < $2
                     AND to2.branch_id = $3
                     AND tc.status != 'cancelled'""",
                ps, d_to_inclusive, location_id,
            )
        else:
            order_rows = await conn.fetch(
                """SELECT paid, status, total
                   FROM orders
                   WHERE created_at >= $1 AND created_at < $2""",
                ps, d_to_inclusive,
            )
            check_rows = await conn.fetch(
                """SELECT tc.status, tc.total
                   FROM table_checks tc
                   WHERE tc.created_at >= $1 AND tc.created_at < $2
                     AND tc.status != 'cancelled'""",
                ps, d_to_inclusive,
            )

    counts: dict[str, int] = {"paid": 0, "pending": 0, "disputed": 0, "courtesy": 0}

    for row in order_rows:
        bucket = _payment_bucket(bool(row["paid"]), row["status"], row["total"])
        counts[bucket] += 1

    for row in check_rows:
        # table_checks: invoiced = paid, everything else = pending/courtesy
        s = (row["status"] or "").lower()
        t = float(row["total"] or 0)
        if t == 0:
            counts["courtesy"] += 1
        elif s == "invoiced":
            counts["paid"] += 1
        else:
            counts["pending"] += 1

    total_count = sum(counts.values())
    buckets = []
    for key in ("paid", "pending", "disputed", "courtesy"):
        c = counts[key]
        pct = round(c / total_count * 100, 1) if total_count else 0.0
        buckets.append({
            "key":   key,
            "label": _PAYMENT_LABELS[key],
            "count": c,
            "pct":   pct,
        })

    return {
        "period":      {"start": period_start, "end": period_end},
        "buckets":     buckets,
        "total_count": total_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. Customers at risk (wrap marketing_repo)
# ─────────────────────────────────────────────────────────────────────────────

async def db_customers_at_risk(org_id: int, limit: int = 50) -> dict:
    """Return at-risk frequent customers (dormant >= 21 days, min 3 orders).

    Wraps marketing_repo.get_at_risk_customers and normalises the response
    shape to match the documented API contract.
    """
    from app.repositories.marketing_repo import get_at_risk_customers  # noqa: PLC0415

    limit = max(1, min(200, limit))
    rows = await get_at_risk_customers(restaurant_id=org_id, limit=limit)

    customers = []
    for r in rows:
        customers.append({
            "phone":        r["customer_phone"],
            "name":         r["customer_name"],
            "total_orders": r["total_orders"],
            "last_seen":    r["last_order_at"],
            "days_since":   r["days_since"],
            "total_spent":  r["total_spent"],  # already quantized float (JSON boundary in marketing_repo)
        })

    return {"count": len(customers), "customers": customers}


# ─────────────────────────────────────────────────────────────────────────────
# 7. Staff performance sparkline
# ─────────────────────────────────────────────────────────────────────────────

async def db_staff_performance(
    org_id: int,
    staff_id: str,
    weeks: int = 7,
) -> dict:
    """Weekly sales totals for a single staff member (mesero/cajero).

    Source: table_orders.waiter_staff_id (added in migration 0039).

    Note: orders table (delivery/pickup) has no direct staff author column,
    so this metric covers salon table orders only.  Documented intentionally.

    Tenant-scope: staff_id must belong to the current org. Validated by
    checking staff.org_id = org_id before querying.
    Returns empty weeks array if staff not found or has no activity.
    """
    from datetime import timedelta as _td, date as _date  # noqa: PLC0415
    from uuid import UUID  # noqa: PLC0415

    weeks = max(1, min(26, weeks))

    # Validate staff_id belongs to org (before any data query)
    try:
        _uuid = str(UUID(staff_id))  # validate UUID format
    except (ValueError, AttributeError):
        return {"staff_id": staff_id, "staff_name": None, "weeks": []}

    async with _tenant_connection() as conn:
        staff_row = await conn.fetchrow(
            "SELECT name FROM staff WHERE id = $1::uuid AND org_id = $2",
            _uuid, org_id,
        )
        if not staff_row:
            return {"staff_id": staff_id, "staff_name": None, "weeks": []}

        staff_name = staff_row["name"]

        # Compute period: last `weeks` complete/current weeks ending today (Mon-based)
        today = _date.today()
        # Start of current week (Monday)
        days_since_monday = today.weekday()  # 0=Mon
        current_week_start = today - _td(days=days_since_monday)
        period_start = current_week_start - _td(weeks=weeks - 1)

        rows = await conn.fetch(
            """SELECT
                   DATE_TRUNC('week', created_at AT TIME ZONE 'UTC')::date AS week_start,
                   COALESCE(SUM(total), 0)::numeric AS sales_total,
                   COUNT(*)::int AS tickets_count
               FROM table_orders
               WHERE waiter_staff_id = $1::uuid
                 AND org_id = $2
                 AND created_at >= $3::date
                 AND status NOT IN ('cancelado')
               GROUP BY 1
               ORDER BY 1 ASC""",
            _uuid, org_id, str(period_start),
        )

        # NPS per-waiter — unblocked by migration 0053 (nps_responses.table_session_id FK).
        # Attribution paths (either counts):
        #   (a) table_sessions.assigned_staff_id explicitly set at session start
        #   (b) any table_orders in the session window authored by this waiter_staff_id
        # We OR them with DISTINCT to avoid double-counting a single NPS response.
        nps_row = await conn.fetchrow(
            """SELECT AVG(n.score)::numeric AS avg_score, COUNT(*)::int AS n_count
               FROM nps_responses n
               WHERE n.org_id = $2
                 AND n.created_at >= $3::date
                 AND n.comment <> '__pending__'
                 AND n.table_session_id IS NOT NULL
                 AND (
                   EXISTS (
                     SELECT 1 FROM table_sessions ts
                      WHERE ts.id = n.table_session_id
                        AND ts.assigned_staff_id = $1::uuid
                   )
                   OR EXISTS (
                     SELECT 1
                       FROM table_sessions ts2
                       JOIN table_orders tord
                         ON tord.table_id = ts2.table_id
                        AND tord.created_at >= ts2.started_at
                        AND tord.created_at <= COALESCE(ts2.ended_at, NOW())
                      WHERE ts2.id = n.table_session_id
                        AND tord.waiter_staff_id = $1::uuid
                   )
                 )""",
            _uuid, org_id, str(period_start),
        )

    # Build complete week series (fill gaps with 0)
    week_map: dict[str, dict] = {}
    for row in rows:
        ws = str(row["week_start"])
        week_map[ws] = {
            "week_start":    ws,
            "sales_total":   int(quantize_money(to_decimal(row["sales_total"]))),  # JSON boundary
            "tickets_count": int(row["tickets_count"]),
        }

    week_series = []
    for i in range(weeks):
        ws = str(current_week_start - _td(weeks=weeks - 1 - i))
        week_series.append(week_map.get(ws, {
            "week_start":    ws,
            "sales_total":   0,
            "tickets_count": 0,
        }))

    # Aggregated rollup across the full period
    tables_served = sum(w["tickets_count"] for w in week_series)
    total_sales   = sum(w["sales_total"]   for w in week_series)
    avg_ticket    = int(total_sales / tables_served) if tables_served else 0

    # NPS average: None if no attributable responses in the window — UI renders "—".
    # Rounded to 1 decimal for display ("4.7") rather than the raw DB precision.
    nps_avg = None
    nps_count = int(nps_row["n_count"]) if nps_row and nps_row["n_count"] else 0
    if nps_count > 0 and nps_row["avg_score"] is not None:
        nps_avg = round(float(nps_row["avg_score"]), 1)

    return {
        "staff_id":     staff_id,
        "staff_name":   staff_name,
        "weeks":        week_series,
        # Rollup fields for the performance card
        "tables_served": tables_served,
        "total_sales":   total_sales,
        "avg_ticket":    avg_ticket,
        "nps_average":   nps_avg,
        "nps_count":     nps_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. Tips pool summary
# ─────────────────────────────────────────────────────────────────────────────

def _current_week_bounds() -> tuple[str, str]:
    """Return (monday_iso, sunday_iso) for the current week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return str(monday), str(sunday)


async def db_tips_pool(
    org_id: int,
    location_id: int,
    period_start: str | None,
    period_end: str | None,
    branch_id: int | None = None,
    caller_staff_id: str | None = None,
) -> dict:
    """Summarise the tip pool for a period.

    Wraps staff_repo.db_calculate_tips_by_attendance.
    Returns top-5 entries_preview, pool_total, entries_count, unallocated,
    and my_pool (tip amount for caller_staff_id, if provided).
    Default period: current week (Mon–Sun).
    """
    from app.repositories.staff_repo import db_calculate_tips_by_attendance  # noqa: PLC0415

    if not period_start or not period_end:
        period_start, period_end = _current_week_bounds()

    result = await db_calculate_tips_by_attendance(
        restaurant_id=location_id,
        period_start=period_start,
        period_end=period_end,
        branch_id=branch_id,
    )

    entries = result.get("entries", [])
    total_tips = float(result.get("total_tips", 0))
    unallocated = float(result.get("unallocated", 0))

    # Top 5 preview with pct
    top5 = entries[:5]
    preview = []
    for e in top5:
        tip_amt = float(e.get("total_tips", 0))
        pct = round(tip_amt / total_tips * 100, 1) if total_tips else 0.0
        preview.append({
            "staff_id":  e.get("staff_id"),
            "name":      e.get("name"),
            "role":      e.get("role"),
            "total_tips": tip_amt,
            "pct":        pct,
        })

    # my_pool: tip allocation for the requesting staff member
    my_pool: float | None = None
    if caller_staff_id:
        for e in entries:
            if str(e.get("staff_id", "")) == str(caller_staff_id):
                my_pool = float(e.get("total_tips", 0))
                break
        if my_pool is None:
            my_pool = 0.0  # caller had no allocations this period

    return {
        "period":          {"start": period_start, "end": period_end},
        "pool_total":      total_tips,
        "entries_count":   len(entries),
        "entries_preview": preview,
        "unallocated":     unallocated,
        "my_pool":         my_pool,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 9. Churn summary  (GET /api/stats/churn-summary)
# ─────────────────────────────────────────────────────────────────────────────

async def db_churn_summary(org_id: int) -> dict:
    """Aggregate churn risk buckets for the clientes-riesgo page.

    Bins customers by churn_score derived from recency (days_since_last_order):
      high   — score >= 0.80  (dormant >= 56 days for ≥3-order customers)
      medium — 0.50 <= score < 0.80  (dormant 21–55 days, ≥3 orders)
      watch  — 0.30 <= score < 0.50  (dormant 14–20 days, ≥2 orders)

    Score formula:  min(1.0, days_since / 70.0)  — linear ramp, caps at 1.0.
    Threshold mapping:
      days >= 56  → score ≥ 0.80  → high
      days >= 35  → score ≥ 0.50  → medium
      days >= 21  → score ≥ 0.30  → watch (≥2 orders threshold to include newer customers)

    ltv_sum: sum of total_spent for high + medium bins. DB NUMERIC → Decimal → float.
    reactivated_count: customers who were dormant (>21d) but have a recent order in
        last 30 days.
        # TODO: customer_profiles.last_seen is updated on every order; there is no
        # "previously_dormant" flag in the current schema.  Counting customers whose
        # last_seen is in the last 30 days AND who had been dormant before requires
        # order-level history which customer_profiles does not expose.  Returning 0
        # until a dedicated "reactivation_events" table or a second latest_order_date
        # column is added.

    medium_risk: top 6 from the medium bin, ordered by churn_score DESC.
    """
    from datetime import date as _date, timedelta as _td  # noqa: PLC0415

    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT
                phone,
                COALESCE(display_name, phone)                              AS name,
                total_orders,
                total_spent,
                EXTRACT(DAY FROM NOW() - last_seen)::INT                   AS days_since,
                last_seen
            FROM customer_profiles
            WHERE org_id       = $1
              AND total_orders >= 2
              AND last_seen    <  NOW() - INTERVAL '14 days'
            ORDER BY last_seen ASC
            """,
            org_id,
        )

    high_count = 0
    medium_count = 0
    watch_count = 0
    ltv_high_medium = Decimal("0")
    medium_risk_rows: list[dict] = []

    for r in rows:
        days = int(r["days_since"] or 0)
        score = min(1.0, days / 70.0)
        total_orders = int(r["total_orders"] or 0)

        # Enforce minimum order thresholds per bin
        if score >= 0.80 and total_orders >= 3:
            high_count += 1
            ltv_high_medium += to_decimal(r["total_spent"])
        elif score >= 0.50 and total_orders >= 3:
            medium_count += 1
            ltv_high_medium += to_decimal(r["total_spent"])
            medium_risk_rows.append({
                "name":         r["name"],
                "phone":        r["phone"],
                "churn_score":  round(score, 4),
                "days_since":   days,
                "total_visits": total_orders,
            })
        elif score >= 0.30 and total_orders >= 2:
            watch_count += 1

    # Sort medium bin by score DESC, take top 6
    medium_risk_rows.sort(key=lambda x: -x["churn_score"])
    medium_risk_top6 = medium_risk_rows[:6]

    return {
        "high_count":       high_count,
        "medium_count":     medium_count,
        "watch_count":      watch_count,
        "ltv_sum":          float(quantize_money(ltv_high_medium)),  # JSON boundary
        "reactivated_count": 0,  # TODO: requires order-history or reactivation_events table
        "medium_risk":      medium_risk_top6,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 10. Branches consolidated  (GET /api/stats/branches-consolidated)
# ─────────────────────────────────────────────────────────────────────────────

async def db_branches_consolidated(org_id: int, days: int = 7) -> dict:
    """Cross-location KPI roll-up for the sucursales page.

    All queries are org-scoped via RLS (org_id GUC) — no explicit WHERE needed,
    but we include the positional parameter for clarity and defence-in-depth.

    Computes:
      total_sales   — SUM(orders.total WHERE paid=TRUE) + SUM(table_orders.total)
                      for the last `days` days.
      total_tickets — COUNT of rows from both tables in the same window.
      avg_nps       — AVG(score) from nps_responses in last 30 days. null if no data.
      total_staff   — COUNT(*) from staff WHERE active = true.
      growth_yoy    — (sales last 30d) / (sales in same 30d window 1 year ago).
                      null if the prior-year window has zero sales.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td  # noqa: PLC0415

    days = max(1, min(365, days))
    now = _dt.now(_tz.utc)
    window_start = now - _td(days=days)

    # 30-day window for NPS and YoY calculations
    month_start = now - _td(days=30)
    yoy_current_start = now - _td(days=30)
    yoy_current_end   = now
    yoy_prior_start   = now - _td(days=395)  # 365+30 days back
    yoy_prior_end     = now - _td(days=365)  # 365 days back

    async with _tenant_connection() as conn:
        # ── sales + tickets from delivery/pickup orders ─────────────────────
        order_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(total), 0)::numeric  AS sales,
                COUNT(*)::int                      AS tickets
            FROM orders
            WHERE paid = TRUE
              AND created_at >= $1
              AND created_at <  $2
              AND org_id = $3
            """,
            window_start, now, org_id,
        )

        # ── sales + tickets from table orders ────────────────────────────────
        table_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(total), 0)::numeric  AS sales,
                COUNT(*)::int                      AS tickets
            FROM table_orders
            WHERE status NOT IN ('cancelado')
              AND created_at >= $1
              AND created_at <  $2
              AND org_id = $3
            """,
            window_start, now, org_id,
        )

        # ── NPS average (last 30 days) ────────────────────────────────────────
        nps_row = await conn.fetchrow(
            """
            SELECT AVG(score)::numeric AS avg_nps
            FROM nps_responses
            WHERE org_id = $1
              AND created_at >= $2
            """,
            org_id, month_start,
        )

        # ── Active staff count ────────────────────────────────────────────────
        staff_count = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM staff
            WHERE org_id = $1
              AND active  = TRUE
            """,
            org_id,
        )

        # ── YoY growth: sales current 30d vs same 30d window last year ───────
        yoy_current_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(o.total), 0)::numeric +
                COALESCE((SELECT SUM(t.total)
                          FROM table_orders t
                          WHERE t.status NOT IN ('cancelado')
                            AND t.created_at >= $1
                            AND t.created_at <  $2
                            AND t.org_id = $3), 0)::numeric AS sales_now
            FROM orders o
            WHERE o.paid = TRUE
              AND o.created_at >= $1
              AND o.created_at <  $2
              AND o.org_id = $3
            """,
            yoy_current_start, yoy_current_end, org_id,
        )

        yoy_prior_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(o.total), 0)::numeric +
                COALESCE((SELECT SUM(t.total)
                          FROM table_orders t
                          WHERE t.status NOT IN ('cancelado')
                            AND t.created_at >= $1
                            AND t.created_at <  $2
                            AND t.org_id = $3), 0)::numeric AS sales_prev
            FROM orders o
            WHERE o.paid = TRUE
              AND o.created_at >= $1
              AND o.created_at <  $2
              AND o.org_id = $3
            """,
            yoy_prior_start, yoy_prior_end, org_id,
        )

    total_sales = (
        to_decimal(order_row["sales"]) + to_decimal(table_row["sales"])
    )
    total_tickets = int(order_row["tickets"]) + int(table_row["tickets"])

    avg_nps_val = nps_row["avg_nps"] if nps_row else None
    avg_nps = round(float(avg_nps_val), 1) if avg_nps_val is not None else None

    sales_now  = to_decimal(yoy_current_row["sales_now"])  if yoy_current_row  else Decimal("0")
    sales_prev = to_decimal(yoy_prior_row["sales_prev"])   if yoy_prior_row    else Decimal("0")

    growth_yoy: float | None = None
    if sales_prev > 0:
        growth_yoy = round(float((sales_now - sales_prev) / sales_prev * 100), 1)

    period_label = f"Últimos {days}d"

    return {
        "period":         period_label,
        "total_sales":    float(quantize_money(total_sales)),    # JSON boundary
        "total_tickets":  total_tickets,
        "avg_nps":        avg_nps,
        "total_staff":    int(staff_count or 0),
        "growth_yoy":     growth_yoy,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 11. Branches comparison  (GET /api/stats/branches-comparison)
# ─────────────────────────────────────────────────────────────────────────────

async def db_branches_comparison(org_id: int, days: int = 30) -> dict:
    """Per-location metric comparison matrix for the sucursales page.

    Fetches all locations for the org, then computes a set of metrics per location.
    Metrics without a clean data source return null with a TODO comment.

    Supported metrics (with real queries):
      Ventas diarias promedio  — total_sales / days per location
      Ticket promedio          — avg order total per location
      NPS                      — avg score from nps_responses per location (last 30d)
      Tasa de reserva confirmada — confirmed / total reservations
      No-show rate             — no_show / total reservations

    Metrics returning null (pending data sources):
      Rotación mesas / día     — TODO: requires table_sessions with open/close timestamps
      Food cost %              — TODO: requires recipe cost telemetry (dish_recipes.cost_pct or similar)
      Costo nómina / ventas    — TODO: requires payroll_runs linked to sales period
      Rotación de personal (12m) — TODO: requires staff.termination_date or departure_events table
      Crecimiento YoY          — TODO: computed at org level in branches_consolidated; per-location
                                       requires historical order data with location_id (available
                                       but not yet back-filled uniformly for all orgs)
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td  # noqa: PLC0415

    days = max(1, min(365, days))
    now = _dt.now(_tz.utc)
    window_start = now - _td(days=days)
    nps_start    = now - _td(days=30)

    async with _tenant_connection() as conn:
        # ── Fetch all locations for this org ──────────────────────────────────
        location_rows = await conn.fetch(
            """
            SELECT id, name
            FROM locations
            WHERE org_id = $1
            ORDER BY id ASC
            """,
            org_id,
        )

        if not location_rows:
            return {"locations": [], "rows": []}

        location_ids = [r["id"] for r in location_rows]
        locations = [{"id": r["id"], "name": r["name"]} for r in location_rows]

        # ── Sales per location (orders + table_orders) ────────────────────────
        order_sales = await conn.fetch(
            """
            SELECT
                location_id,
                COALESCE(SUM(total), 0)::numeric  AS sales,
                COUNT(*)::int                      AS tickets
            FROM orders
            WHERE paid = TRUE
              AND created_at >= $1
              AND org_id = $2
            GROUP BY location_id
            """,
            window_start, org_id,
        )

        table_sales = await conn.fetch(
            """
            SELECT
                location_id,
                COALESCE(SUM(total), 0)::numeric  AS sales,
                COUNT(*)::int                      AS tickets
            FROM table_orders
            WHERE status NOT IN ('cancelado')
              AND created_at >= $1
              AND org_id = $2
            GROUP BY location_id
            """,
            window_start, org_id,
        )

        # ── NPS per location (last 30d) ───────────────────────────────────────
        nps_rows = await conn.fetch(
            """
            SELECT
                location_id,
                AVG(score)::numeric AS avg_score
            FROM nps_responses
            WHERE org_id = $1
              AND created_at >= $2
            GROUP BY location_id
            """,
            org_id, nps_start,
        )

        # ── Reservation stats per location ────────────────────────────────────
        res_rows = await conn.fetch(
            """
            SELECT
                branch_id                                     AS location_id,
                COUNT(*)::int                                 AS total_res,
                COUNT(*) FILTER (WHERE status = 'confirmed')::int AS confirmed_res,
                COUNT(*) FILTER (WHERE no_show = TRUE)::int       AS no_shows
            FROM reservations
            WHERE org_id = $1
              AND created_at >= $2
            GROUP BY branch_id
            """,
            org_id, window_start,
        )

    # ── Build per-location lookup maps ────────────────────────────────────────
    order_by_loc: dict[int, dict] = {}
    for r in order_sales:
        lid = r["location_id"]
        if lid is None:
            continue
        d = order_by_loc.setdefault(lid, {"sales": Decimal("0"), "tickets": 0})
        d["sales"]   += to_decimal(r["sales"])
        d["tickets"] += int(r["tickets"])

    for r in table_sales:
        lid = r["location_id"]
        if lid is None:
            continue
        d = order_by_loc.setdefault(lid, {"sales": Decimal("0"), "tickets": 0})
        d["sales"]   += to_decimal(r["sales"])
        d["tickets"] += int(r["tickets"])

    nps_by_loc: dict[int, float | None] = {}
    for r in nps_rows:
        lid = r["location_id"]
        if lid is None:
            continue
        nps_by_loc[lid] = round(float(r["avg_score"]), 1) if r["avg_score"] is not None else None

    res_by_loc: dict[int, dict] = {}
    for r in res_rows:
        lid = r["location_id"]
        if lid is None:
            continue
        res_by_loc[lid] = {
            "total":     int(r["total_res"]),
            "confirmed": int(r["confirmed_res"]),
            "no_shows":  int(r["no_shows"]),
        }

    # ── Per-location metric arrays ────────────────────────────────────────────
    daily_avg_per_loc: list[float | None]  = []
    avg_ticket_per_loc: list[float | None] = []
    nps_per_loc: list[float | None]        = []
    conf_rate_per_loc: list[float | None]  = []
    noshow_rate_per_loc: list[float | None]= []

    for loc in locations:
        lid = loc["id"]
        od  = order_by_loc.get(lid, {"sales": Decimal("0"), "tickets": 0})
        sales   = od["sales"]
        tickets = od["tickets"]

        daily_avg_per_loc.append(
            float(quantize_money(sales / days)) if days > 0 else None  # JSON boundary
        )
        avg_ticket_per_loc.append(
            float(quantize_money(sales / tickets)) if tickets > 0 else None  # JSON boundary
        )
        nps_per_loc.append(nps_by_loc.get(lid))

        rd = res_by_loc.get(lid)
        if rd and rd["total"] > 0:
            conf_rate_per_loc.append(round(rd["confirmed"] / rd["total"] * 100, 1))
            noshow_rate_per_loc.append(round(rd["no_shows"] / rd["total"] * 100, 1))
        else:
            conf_rate_per_loc.append(None)
            noshow_rate_per_loc.append(None)

    def _build_row(metric: str, per_location: list, target: float | None = None) -> dict:
        """Build a comparison row, computing avg and top_location_id."""
        non_null = [v for v in per_location if v is not None]
        avg = round(sum(non_null) / len(non_null), 1) if non_null else None

        top_idx = None
        if non_null:
            top_val = max(non_null)
            for i, v in enumerate(per_location):
                if v == top_val:
                    top_idx = i
                    break

        top_location_id = locations[top_idx]["id"] if top_idx is not None else None

        vs_target_pct: float | None = None
        if avg is not None and target and target > 0:
            vs_target_pct = round((avg - target) / target * 100, 1)

        return {
            "metric":          metric,
            "per_location":    per_location,
            "avg":             avg,
            "target":          target,
            "top_location_id": top_location_id,
            "vs_target_pct":   vs_target_pct,
        }

    comparison_rows = [
        _build_row("Ventas diarias promedio",       daily_avg_per_loc),
        _build_row("Ticket promedio",                avg_ticket_per_loc),
        # TODO: Rotación mesas / día — requires table_sessions open/close timestamps per location
        _build_row("Rotación mesas / día",           [None] * len(locations)),
        _build_row("NPS",                            nps_per_loc),
        # TODO: Food cost % — requires dish_recipes cost telemetry (cost_per_unit in inventory)
        _build_row("Food cost %",                    [None] * len(locations)),
        # TODO: Costo nómina / ventas — requires payroll_runs joined to a period matching sales window
        _build_row("Costo nómina / ventas",          [None] * len(locations)),
        _build_row("Tasa de reserva confirmada",     conf_rate_per_loc),
        _build_row("No-show rate",                   noshow_rate_per_loc),
        # TODO: Rotación de personal (12m) — requires staff.termination_date or departure events
        _build_row("Rotación de personal (12m)",     [None] * len(locations)),
        # TODO: Crecimiento YoY per-location — available in orders.location_id but needs
        #       uniform backfill validation per org before exposing as a reliable metric
        _build_row("Crecimiento YoY",                [None] * len(locations)),
    ]

    return {
        "locations": locations,
        "rows":      comparison_rows,
    }
