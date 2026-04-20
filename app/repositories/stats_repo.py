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

    return {
        "staff_id":   staff_id,
        "staff_name": staff_name,
        "weeks":      week_series,
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
) -> dict:
    """Summarise the tip pool for a period.

    Wraps staff_repo.db_calculate_tips_by_attendance.
    Returns top-5 entries_preview, pool_total, entries_count, unallocated.
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

    return {
        "period":          {"start": period_start, "end": period_end},
        "pool_total":      total_tips,
        "entries_count":   len(entries),
        "entries_preview": preview,
        "unallocated":     unallocated,
    }
