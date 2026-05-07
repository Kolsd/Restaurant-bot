"""
app/repositories/cost_metrics_repo.py

Cross-tenant cost analytics for the Mesio internal team.
All functions here require bypass_tenant_scope("internal_cost_dashboard")
at the call site — they intentionally read across ALL organizations.

Schema read from:
  subscription_usage: org_id, usage_date, total_tokens, orders_count, updated_at
  organizations: id, name, subscription_plan, subscription_status

Cost computation is delegated to app.services.cost_estimator (pure, no DB).
Float appears ONLY at JSON boundary — all intermediate math is Decimal.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.services.logging import get_logger

log = get_logger(__name__)


def _get_pool():
    from app.services.database import get_pool
    return get_pool()


def _estimate(tokens: int):
    """Lazy import so cost_estimator env vars are read at call time."""
    from app.services.cost_estimator import estimate_cost_usd, estimate_cost_cop
    return estimate_cost_usd(tokens), estimate_cost_cop(tokens)


# ── Platform-wide summary ─────────────────────────────────────────────────────


async def db_platform_cost_summary(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Total platform token spend and estimated cost for [start_date, end_date].

    Requires bypass_tenant_scope("internal_cost_dashboard") at call site.

    Returns:
    {
        "total_tokens": int,
        "estimated_cost_usd": float,   # JSON boundary
        "estimated_cost_cop": float,   # JSON boundary
        "days": int,
        "by_day": [{"date": "YYYY-MM-DD", "tokens": int, "cost_usd": float}]
    }
    """
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    usage_date,
                    SUM(total_tokens)::BIGINT AS tokens
                FROM subscription_usage
                WHERE usage_date BETWEEN $1 AND $2
                GROUP BY usage_date
                ORDER BY usage_date
                """,
                start_date,
                end_date,
            )
    except Exception as exc:
        log.exception("cost_metrics.platform_summary.query_error", exc_type=type(exc).__name__)
        rows = []

    total_tokens = 0
    by_day = []
    for row in rows:
        t = int(row["tokens"] or 0)
        total_tokens += t
        cost_usd, _ = _estimate(t)
        by_day.append({
            "date": str(row["usage_date"]),
            "tokens": t,
            "cost_usd": float(cost_usd),  # JSON boundary
        })

    total_cost_usd, total_cost_cop = _estimate(total_tokens)
    days = max((end_date - start_date).days + 1, 1)

    return {
        "total_tokens": total_tokens,
        "estimated_cost_usd": float(total_cost_usd),  # JSON boundary
        "estimated_cost_cop": float(total_cost_cop),  # JSON boundary
        "days": days,
        "by_day": by_day,
    }


# ── Per-restaurant ranking ────────────────────────────────────────────────────


async def db_per_restaurant_costs(
    start_date: date,
    end_date: date,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Top N restaurants by token spend in [start_date, end_date].

    Requires bypass_tenant_scope("internal_cost_dashboard") at call site.

    Each row:
    {
        "org_id": int,
        "org_name": str,
        "plan_code": str,
        "total_tokens": int,
        "estimated_cost_usd": float,   # JSON boundary
        "estimated_cost_cop": float,   # JSON boundary
        "orders_count": int,
    }
    """
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    su.org_id,
                    o.name                             AS org_name,
                    COALESCE(o.subscription_plan, 'free') AS plan_code,
                    SUM(su.total_tokens)::BIGINT       AS total_tokens,
                    SUM(su.orders_count)::BIGINT       AS orders_count
                FROM subscription_usage su
                LEFT JOIN organizations o ON o.id = su.org_id
                WHERE su.usage_date BETWEEN $1 AND $2
                GROUP BY su.org_id, o.name, o.subscription_plan
                ORDER BY total_tokens DESC
                LIMIT $3
                """,
                start_date,
                end_date,
                limit,
            )
    except Exception as exc:
        log.exception("cost_metrics.per_restaurant.query_error", exc_type=type(exc).__name__)
        rows = []

    result = []
    for row in rows:
        tokens = int(row["total_tokens"] or 0)
        cost_usd, cost_cop = _estimate(tokens)
        result.append({
            "org_id": row["org_id"],
            "org_name": row["org_name"] or f"Org #{row['org_id']}",
            "plan_code": row["plan_code"] or "free",
            "total_tokens": tokens,
            "estimated_cost_usd": float(cost_usd),  # JSON boundary
            "estimated_cost_cop": float(cost_cop),  # JSON boundary
            "orders_count": int(row["orders_count"] or 0),
        })
    return result


# ── Single restaurant detail ──────────────────────────────────────────────────


async def db_restaurant_cost_detail(
    org_id: int,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Daily token breakdown for one restaurant.

    Requires bypass_tenant_scope("internal_cost_dashboard") at call site.

    Returns:
    {
        "org_id": int,
        "org_name": str,
        "plan_code": str,
        "by_day": [{"date": str, "tokens": int, "cost_usd": float, "orders_count": int}],
        "totals": {"tokens": int, "cost_usd": float, "cost_cop": float, "orders_count": int},
    }
    """
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            org_row = await conn.fetchrow(
                "SELECT name, COALESCE(subscription_plan, 'free') AS plan FROM organizations WHERE id = $1",
                org_id,
            )
            rows = await conn.fetch(
                """
                SELECT
                    usage_date,
                    COALESCE(total_tokens, 0)  AS tokens,
                    COALESCE(orders_count, 0)  AS orders_count
                FROM subscription_usage
                WHERE org_id = $1
                  AND usage_date BETWEEN $2 AND $3
                ORDER BY usage_date
                """,
                org_id,
                start_date,
                end_date,
            )
    except Exception as exc:
        log.exception("cost_metrics.detail.query_error", org_id=org_id, exc_type=type(exc).__name__)
        org_row = None
        rows = []

    by_day = []
    total_tokens = 0
    total_orders = 0
    for row in rows:
        t = int(row["tokens"] or 0)
        o = int(row["orders_count"] or 0)
        total_tokens += t
        total_orders += o
        cost_usd, _ = _estimate(t)
        by_day.append({
            "date": str(row["usage_date"]),
            "tokens": t,
            "cost_usd": float(cost_usd),  # JSON boundary
            "orders_count": o,
        })

    total_usd, total_cop = _estimate(total_tokens)

    return {
        "org_id": org_id,
        "org_name": (org_row["name"] if org_row else None) or f"Org #{org_id}",
        "plan_code": (org_row["plan"] if org_row else None) or "free",
        "by_day": by_day,
        "totals": {
            "tokens": total_tokens,
            "cost_usd": float(total_usd),   # JSON boundary
            "cost_cop": float(total_cop),   # JSON boundary
            "orders_count": total_orders,
        },
    }


# ── Outlier detection ─────────────────────────────────────────────────────────

# Expected token consumption per conversation (tuned from real data).
# At $0.60/Mtok, 2000 tokens/conv ≈ $0.0012/conv — used to compute "plan cap cost".
_TOKENS_PER_CONV_ESTIMATE = 2_000

# Plan conv caps (daily limit analogue — we use daily_tokens / tokens_per_conv).
# Plan codes mirror migration 0070 (Pricing v1): pulso/restaurante/pro/cadena/comp.
# "free" kept as fallback for pre-pricing orgs; "basic"/"enterprise" removed (not real plans).
_PLAN_DAILY_TOKEN_LIMITS = {
    "free":        5_000,
    "pulso":       50_000,
    "restaurante": 200_000,
    "pro":         600_000,
    "cadena":      -1,    # unlimited — excluded from outlier detection
    "comp":        -1,    # Mesio comp accounts — excluded from outlier detection
}


async def db_cost_outliers(
    start_date: date,
    end_date: date,
    threshold_pct: int = 200,
) -> list[dict[str, Any]]:
    """Restaurants whose average daily token spend exceeds threshold_pct% of plan limit.

    threshold_pct=200 means the restaurant is burning 2× its plan's daily allowance.

    Enterprise orgs are excluded (unlimited plan).
    Requires bypass_tenant_scope("internal_cost_dashboard") at call site.

    Each row:
    {
        "org_id": int,
        "org_name": str,
        "plan_code": str,
        "plan_daily_token_limit": int,
        "avg_daily_tokens": float,
        "pct_of_limit": float,
        "total_tokens": int,
        "estimated_cost_usd": float,   # JSON boundary
    }
    """
    pool = await _get_pool()
    days = max((end_date - start_date).days + 1, 1)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    su.org_id,
                    o.name                              AS org_name,
                    COALESCE(o.subscription_plan, 'free') AS plan_code,
                    SUM(su.total_tokens)::BIGINT        AS total_tokens
                FROM subscription_usage su
                LEFT JOIN organizations o ON o.id = su.org_id
                WHERE su.usage_date BETWEEN $1 AND $2
                GROUP BY su.org_id, o.name, o.subscription_plan
                HAVING SUM(su.total_tokens) > 0
                ORDER BY total_tokens DESC
                """,
                start_date,
                end_date,
            )
    except Exception as exc:
        log.exception("cost_metrics.outliers.query_error", exc_type=type(exc).__name__)
        rows = []

    outliers = []
    for row in rows:
        plan = (row["plan_code"] or "free").lower()
        # -1 means unlimited (cadena, comp) — exclude from outlier detection.
        plan_limit = _PLAN_DAILY_TOKEN_LIMITS.get(plan, _PLAN_DAILY_TOKEN_LIMITS["pulso"])
        if plan_limit <= 0:
            continue

        total = int(row["total_tokens"] or 0)
        avg_daily = total / days
        pct = (avg_daily / plan_limit) * 100.0

        if pct >= threshold_pct:
            cost_usd, _ = _estimate(total)
            outliers.append({
                "org_id": row["org_id"],
                "org_name": row["org_name"] or f"Org #{row['org_id']}",
                "plan_code": plan,
                "plan_daily_token_limit": plan_limit,
                "avg_daily_tokens": round(avg_daily, 1),
                "pct_of_limit": round(pct, 1),
                "total_tokens": total,
                "estimated_cost_usd": float(cost_usd),  # JSON boundary
            })

    return outliers
