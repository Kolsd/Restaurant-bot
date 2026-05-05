"""Subscription usage tracking + plan limits resolution.

Schema (subscription_usage, post-migration 0065):
  id, org_id, usage_date,
  total_tokens, total_invoices, orders_count,
  updated_at

UNIQUE (org_id, usage_date) — canonical Wave-2 key.

NOTE: restaurant_id was DROPPED by migration 0037
(0037_drop_legacy_rls_and_restaurant_id.py). All INSERTs must NOT reference it
or asyncpg raises UndefinedColumnError. The legacy
restaurant_repo.db_increment_invoice_usage / db_increment_token_usage already
followed this pattern; the helpers below are aligned with it.

Conventions:
  - All increment helpers are atomic via INSERT … ON CONFLICT DO UPDATE.
  - All readers + writers run inside tenant_connection() and require an
    active tenant_scope() (or bypass) per the multi-tenant RLS rules.
  - org_id is the canonical tenant key; legacy callers that pass
    restaurant_id continue to work for single-location tenants because
    organizations.id == old restaurants.id for every Matriz (migration 0034).
"""

from app.services.tenant_db import tenant_connection
from app.services.logging import get_logger

log = get_logger(__name__)


# ── Plan limit defaults ──────────────────────────────────────────────────────
#
# A value of -1 means "unlimited" (enterprise plan, custom override). Custom
# overrides live in restaurants.features.plan_limits and take precedence over
# the plan defaults when present.

_DEFAULT_LIMITS = {
    "free":       {"daily_tokens": 5_000,    "daily_invoices": 5,   "monthly_orders": 50},
    "basic":      {"daily_tokens": 50_000,   "daily_invoices": 50,  "monthly_orders": 500},
    "pro":        {"daily_tokens": 200_000,  "daily_invoices": 500, "monthly_orders": 5_000},
    "enterprise": {"daily_tokens": -1,       "daily_invoices": -1,  "monthly_orders": -1},
}


def _resolve_limits(features: dict | None, plan: str | None) -> dict:
    """Pull limits from features.plan_limits, falling back to plan defaults.

    Returns a dict with keys: daily_tokens, daily_invoices, monthly_orders.
    A value of -1 means unlimited.

    Unknown plan strings fall back to "basic" (the safe middle).
    """
    plan_key = (plan or "basic").lower()
    plan_defaults = _DEFAULT_LIMITS.get(plan_key, _DEFAULT_LIMITS["basic"])
    custom = {}
    if isinstance(features, dict):
        raw = features.get("plan_limits") or {}
        if isinstance(raw, dict):
            custom = raw
    return {
        "daily_tokens":   int(custom.get("daily_tokens",   plan_defaults["daily_tokens"])),
        "daily_invoices": int(custom.get("daily_invoices", plan_defaults["daily_invoices"])),
        "monthly_orders": int(custom.get("monthly_orders", plan_defaults["monthly_orders"])),
    }


# ── Increment helpers ────────────────────────────────────────────────────────


async def db_increment_tokens(org_id: int, tokens: int) -> int:
    """Atomic increment of total_tokens for today. Returns the new total.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if tokens <= 0:
        return 0
    async with tenant_connection() as conn:
        new_val = await conn.fetchval(
            """INSERT INTO subscription_usage (org_id, usage_date, total_tokens)
               VALUES ($1, CURRENT_DATE, $2)
               ON CONFLICT (org_id, usage_date) DO UPDATE
               SET total_tokens = subscription_usage.total_tokens + $2,
                   updated_at   = NOW()
               RETURNING total_tokens""",
            org_id, tokens,
        )
    return int(new_val or 0)


async def db_increment_invoices(org_id: int) -> int:
    """Atomic increment of total_invoices for today. Returns the new total.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        new_val = await conn.fetchval(
            """INSERT INTO subscription_usage (org_id, usage_date, total_invoices)
               VALUES ($1, CURRENT_DATE, 1)
               ON CONFLICT (org_id, usage_date) DO UPDATE
               SET total_invoices = subscription_usage.total_invoices + 1,
                   updated_at     = NOW()
               RETURNING total_invoices""",
            org_id,
        )
    return int(new_val or 0)


async def db_increment_orders(org_id: int) -> int:
    """Atomic increment of orders_count for today. Returns the running monthly total.

    The monthly total aggregates orders_count across all rows in the current
    calendar month (DATE_TRUNC('month', CURRENT_DATE)).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.fetchval(
            """INSERT INTO subscription_usage (org_id, usage_date, orders_count)
               VALUES ($1, CURRENT_DATE, 1)
               ON CONFLICT (org_id, usage_date) DO UPDATE
               SET orders_count = subscription_usage.orders_count + 1,
                   updated_at   = NOW()
               RETURNING orders_count""",
            org_id,
        )
        monthly = await conn.fetchval(
            """SELECT COALESCE(SUM(orders_count), 0) FROM subscription_usage
               WHERE org_id = $1 AND usage_date >= DATE_TRUNC('month', CURRENT_DATE)""",
            org_id,
        )
    return int(monthly or 0)


# ── Read helpers ─────────────────────────────────────────────────────────────


async def db_get_usage_summary(org_id: int) -> dict:
    """Today's usage + monthly aggregates for admin display.

    Returns:
      {
        "today": {"tokens_used": int, "invoices_used": int, "orders_count": int},
        "month": {"orders_count": int}
      }

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        today = await conn.fetchrow(
            """SELECT total_tokens, total_invoices, orders_count
               FROM subscription_usage
               WHERE org_id = $1 AND usage_date = CURRENT_DATE""",
            org_id,
        )
        monthly_orders = await conn.fetchval(
            """SELECT COALESCE(SUM(orders_count), 0) FROM subscription_usage
               WHERE org_id = $1 AND usage_date >= DATE_TRUNC('month', CURRENT_DATE)""",
            org_id,
        )
    today_d = dict(today) if today else {}
    return {
        "today": {
            "tokens_used":   int(today_d.get("total_tokens",   0) or 0),
            "invoices_used": int(today_d.get("total_invoices", 0) or 0),
            "orders_count":  int(today_d.get("orders_count",   0) or 0),
        },
        "month": {
            "orders_count": int(monthly_orders or 0),
        },
    }
