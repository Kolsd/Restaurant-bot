"""
Marketing repository — Marketing system v1.

Handles:
  - Plan-based marketing caps (basic=30, pro=300, enterprise=unlimited)
  - marketing_messages_log CRUD
  - At-risk customer detection (dormant frequent customers)
  - marketing_enabled toggle

All monetary values use Decimal end-to-end. float() only at JSON boundary in routes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.services.logging import get_logger
from app.services.money import to_decimal, quantize_money

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MARKETING_CAPS: dict[str, Optional[int]] = {
    "basic": 30,
    "pro": 300,
    "enterprise": None,  # None = unlimited
}

# LATAM default cost per marketing conversation (USD)
MARKETING_COST_USD_DEFAULT = Decimal("0.0625")

MARKETING_COSTS_USD_BY_COUNTRY: dict[str, Decimal] = {
    "CO": Decimal("0.0625"),
    "MX": Decimal("0.0436"),
    "AR": Decimal("0.0618"),
    "CL": Decimal("0.0625"),
    "PE": Decimal("0.0625"),
    "ES": Decimal("0.0618"),
    "US": Decimal("0.025"),
    "BR": Decimal("0.0625"),
}

DORMANT_DAYS: int = 21
MIN_DORMANT_ORDERS: int = 3


# ── Churn tiering ─────────────────────────────────────────────────────────────

def churn_tier(days_since_last_order: int, total_orders: int = 0) -> str:
    """Classify customer churn risk based on order history.

    Returns one of: 'active' | 'cooling' | 'at_risk' | 'churned' | 'lost'.

    Bands (days since last order):
      - active:   <= 14 days
      - cooling:  15-30 days
      - at_risk:  31-60 days
      - churned:  61-120 days
      - lost:     > 120 days

    `total_orders` is currently informational (kept in the signature so callers
    can pass it without breaking when we extend the heuristic to consider
    one-shot vs. repeat customers). Negative `days_since_last_order` is
    coerced to 0 (defensive — clock skew across DB/server).
    """
    days = max(0, int(days_since_last_order or 0))
    if days <= 14:
        return "active"
    if days <= 30:
        return "cooling"
    if days <= 60:
        return "at_risk"
    if days <= 120:
        return "churned"
    return "lost"


# ── Lazy pool / serialize accessors ──────────────────────────────────────────

def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _s  # noqa: PLC0415
    return _s(d)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _month_bounds_utc(tz_name: str) -> tuple[datetime, datetime]:
    """Return (month_start_utc, month_end_utc) for the current local month.

    Uses zoneinfo for tz-aware conversion. Falls back to UTC if tz is invalid.
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        tz = ZoneInfo(tz_name or "America/Bogota")
    except Exception:
        tz = timezone.utc

    now_local = datetime.now(tz)
    month_start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Next month first day
    if month_start_local.month == 12:
        month_end_local = month_start_local.replace(year=month_start_local.year + 1, month=1)
    else:
        month_end_local = month_start_local.replace(month=month_start_local.month + 1)

    return (
        month_start_local.astimezone(timezone.utc),
        month_end_local.astimezone(timezone.utc),
    )


async def _get_restaurant_country(restaurant_id: int, conn) -> str:
    """Fetch country code from restaurant features JSONB. Returns 'CO' on failure."""
    try:
        row = await conn.fetchrow(
            "SELECT features FROM restaurants WHERE id = $1",
            restaurant_id,
        )
        if row and row["features"]:
            import json  # noqa: PLC0415
            features = row["features"]
            if isinstance(features, str):
                features = json.loads(features)
            if isinstance(features, dict):
                return (features.get("country_code") or "CO").upper()
    except Exception as exc:
        log.warning("marketing_repo.get_country_failed", restaurant_id=restaurant_id, error=str(exc))
    return "CO"


def _cost_for_country(country_code: str) -> Decimal:
    return MARKETING_COSTS_USD_BY_COUNTRY.get(country_code.upper(), MARKETING_COST_USD_DEFAULT)


# ── Public functions ──────────────────────────────────────────────────────────

async def count_marketing_this_month(restaurant_id: int, tz_name: str) -> int:
    """Count marketing_messages_log rows with status='sent' in the current local month."""
    month_start, month_end = _month_bounds_utc(tz_name)
    async with _tenant_connection() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM marketing_messages_log
            WHERE org_id  = $1
              AND status        = 'sent'
              AND sent_at       >= $2
              AND sent_at       <  $3
            """,
            restaurant_id,
            month_start,
            month_end,
        )
    return int(count or 0)


async def get_marketing_status(restaurant_id: int) -> dict:
    """Return full marketing status for the restaurant.

    Returns:
        {
            plan, cap, sent_this_month, remaining, enabled,
            cost_estimate_month_usd (Decimal)
        }
    remaining is None when cap is None (enterprise = unlimited).
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT plan,
                   marketing_enabled,
                   COALESCE(features->>'timezone', 'America/Bogota') AS tz,
                   COALESCE(features->>'country_code', 'CO')         AS country_code
            FROM restaurants
            WHERE id = $1
            """,
            restaurant_id,
        )

    if not row:
        return {
            "plan": "basic",
            "cap": MARKETING_CAPS["basic"],
            "sent_this_month": 0,
            "remaining": MARKETING_CAPS["basic"],
            "enabled": True,
            "cost_estimate_month_usd": Decimal("0"),
        }

    plan = row["plan"] or "basic"
    enabled = bool(row["marketing_enabled"])
    tz_name = row["tz"] or "America/Bogota"
    country_code = (row["country_code"] or "CO").upper()

    cap = MARKETING_CAPS.get(plan, MARKETING_CAPS["basic"])
    sent = await count_marketing_this_month(restaurant_id, tz_name)
    remaining = None if cap is None else max(0, cap - sent)

    cost_per_msg = _cost_for_country(country_code)
    cost_month = quantize_money(cost_per_msg * Decimal(str(sent)), currency="USD")

    return {
        "plan": plan,
        "cap": cap,
        "sent_this_month": sent,
        "remaining": remaining,
        "enabled": enabled,
        "cost_estimate_month_usd": cost_month,
    }


async def can_send_marketing(restaurant_id: int) -> tuple[bool, str]:
    """Check whether a marketing message can be sent.

    Returns:
        (True,  "ok")                  — allowed
        (False, "disabled")            — marketing_enabled = False
        (False, "cap_reached")         — monthly cap exhausted
        (False, "restaurant_not_found")— restaurant does not exist
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT plan,
                   marketing_enabled,
                   COALESCE(features->>'timezone', 'America/Bogota') AS tz
            FROM restaurants
            WHERE id = $1
            """,
            restaurant_id,
        )

    if not row:
        return False, "restaurant_not_found"

    if not bool(row["marketing_enabled"]):
        return False, "disabled"

    plan = row["plan"] or "basic"
    cap = MARKETING_CAPS.get(plan, MARKETING_CAPS["basic"])

    if cap is None:
        # enterprise — unlimited
        return True, "ok"

    tz_name = row["tz"] or "America/Bogota"
    sent = await count_marketing_this_month(restaurant_id, tz_name)
    if sent >= cap:
        return False, "cap_reached"

    return True, "ok"


async def log_marketing_message(
    restaurant_id: int,
    customer_phone: str,
    message_type: str,
    status: str,
    message_body: Optional[str] = None,
    template_name: Optional[str] = None,
    cost_estimate_usd: Optional[Decimal] = None,
    error: Optional[str] = None,
    meta_message_id: Optional[str] = None,
) -> int:
    """Insert a marketing_messages_log row. Returns the new row id.

    If cost_estimate_usd is None, uses the country default for the restaurant.
    cost_estimate_usd must be Decimal — never float.
    """
    async with _tenant_connection() as conn:
        # Resolve cost if not provided
        if cost_estimate_usd is None:
            country_code = await _get_restaurant_country(restaurant_id, conn)
            cost_estimate_usd = _cost_for_country(country_code)

        # Ensure Decimal precision (defensive)
        cost_estimate_usd = quantize_money(to_decimal(cost_estimate_usd), currency="USD")

        row_id = await conn.fetchval(
            """
            INSERT INTO marketing_messages_log
                (org_id, customer_phone, message_type, template_name,
                 message_body, cost_estimate_usd, status, error, meta_message_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            restaurant_id,
            customer_phone,
            message_type,
            template_name,
            message_body,
            cost_estimate_usd,
            status,
            error,
            meta_message_id,
        )
    return int(row_id)


async def get_at_risk_customers(restaurant_id: int, limit: int = 50) -> list[dict]:
    """Return frequent customers who have not ordered in >= DORMANT_DAYS days.

    Requires customer_profiles table (migration 0021+):
      restaurant_id, phone, name, total_orders, total_spent, last_seen

    Returns list of dicts ordered by days_since DESC (longest dormant first).
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT
                phone                                  AS customer_phone,
                COALESCE(display_name, phone)           AS customer_name,
                last_seen                              AS last_order_at,
                EXTRACT(DAY FROM NOW() - last_seen)::INT AS days_since,
                total_orders,
                total_spent
            FROM customer_profiles
            WHERE org_id         = $1
              AND total_orders   >= $2
              AND last_seen      <  NOW() - MAKE_INTERVAL(days => $3)
            ORDER BY last_seen ASC
            LIMIT $4
            """,
            restaurant_id,
            MIN_DORMANT_ORDERS,
            DORMANT_DAYS,
            limit,
        )

    result = []
    for r in rows:
        days = int(r["days_since"] or 0)
        total_orders = int(r["total_orders"] or 0)
        result.append({
            "customer_phone": r["customer_phone"],
            "customer_name":  r["customer_name"],
            "last_order_at":  r["last_order_at"].isoformat() if r["last_order_at"] else None,
            "days_since":     days,
            "total_orders":   total_orders,
            "total_spent":    float(quantize_money(to_decimal(r["total_spent"]))),  # JSON boundary
            "tier":           churn_tier(days, total_orders),
        })
    return result


async def get_recent_marketing_messages(restaurant_id: int, limit: int = 50) -> list[dict]:
    """Return recent marketing log rows for the restaurant (newest first)."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, customer_phone, message_type, template_name,
                   message_body, sent_at, cost_estimate_usd, status,
                   error, meta_message_id
            FROM marketing_messages_log
            WHERE org_id = $1
            ORDER BY sent_at DESC
            LIMIT $2
            """,
            restaurant_id,
            limit,
        )

    result = []
    for r in rows:
        result.append({
            "id":               int(r["id"]),
            "customer_phone":   r["customer_phone"],
            "message_type":     r["message_type"],
            "template_name":    r["template_name"],
            "message_body":     r["message_body"],
            "sent_at":          r["sent_at"].isoformat() if r["sent_at"] else None,
            "cost_estimate_usd": float(quantize_money(to_decimal(r["cost_estimate_usd"]), currency="USD")),  # JSON boundary
            "status":           r["status"],
            "error":            r["error"],
            "meta_message_id":  r["meta_message_id"],
        })
    return result


async def toggle_marketing_enabled(restaurant_id: int, enabled: bool) -> bool:
    """Set marketing_enabled for a restaurant. Returns True if row was updated."""
    async with _tenant_connection() as conn:
        result = await conn.execute(
            """
            UPDATE restaurants
               SET marketing_enabled = $2
             WHERE id = $1
            """,
            restaurant_id,
            enabled,
        )
    # asyncpg returns "UPDATE N" string
    return result == "UPDATE 1"
