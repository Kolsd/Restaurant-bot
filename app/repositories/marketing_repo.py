"""
At-risk customer detection (dormant frequent customers).

What is left of the WhatsApp marketing module (deleted 2026-09-25); used by
the customers-at-risk stats and ai_insights until those go in the module
cleanup. Monetary values are Decimal; float() only at the JSON boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.services.logging import get_logger
from app.services.money import to_decimal, quantize_money

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

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


# ── Public functions ──────────────────────────────────────────────────────────


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

