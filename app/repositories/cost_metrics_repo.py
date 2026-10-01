"""
app/repositories/cost_metrics_repo.py

Cross-tenant cost analytics for the Mesio internal team.
All functions here require bypass_tenant_scope("internal_cost_dashboard")
at the call site — they intentionally read across ALL organizations.

All queries go through tenant_connection() (never pool.acquire() directly).
tenant_connection() is what actually executes `SET LOCAL ROLE mesio_superadmin`
for the duration of the query when bypass is active — without it, RLS on
subscription_usage (migration 0029) filters by whatever app.org_id happens to
be left on the pooled connection from a previous request, i.e. the result is
non-deterministic connection-reuse garbage, not a real cross-tenant read.

Schema read from:
  subscription_usage: org_id, usage_date, total_tokens, orders_count, updated_at,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_write_tokens (per-kind split, migration 0094)
  organizations: id, name, subscription_plan, subscription_status

Cost computation is delegated to app.services.cost_estimator (pure, no DB).
Float appears ONLY at JSON boundary — all intermediate math is Decimal.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


def _estimate_row(row, tokens: int):
    """Price one aggregated usage row, returning (usd, cop) as Decimals.

    Two eras of data live in this table and they cannot be priced the same
    way. Rows written from migration 0094 on carry the four per-kind token
    counters, each billed at its own rate (output costs 50x what a cache
    read costs) — those are priced exactly. Rows written before it carry
    only `total_tokens`, a sum of uncached input and output with no split
    recorded, so there is nothing better to do than the old blended rate.

    A period that spans the migration gets both: the split part priced per
    kind, and whatever `total_tokens` holds beyond `input + output` treated
    as legacy remainder. The alternative — pricing the whole aggregate one
    way — would either invent a split we never measured or throw away the
    one we now have.

    Lazy import so cost_estimator env vars are read at call time.
    """
    from app.services.cost_estimator import (  # noqa: PLC0415
        estimate_cost_usd,
        estimate_cost_usd_breakdown,
        usd_to_cop,
    )

    def _col(name: str) -> int:
        try:
            return int(row[name] or 0)
        except (KeyError, TypeError, ValueError):
            return 0

    input_tokens       = _col("input_tokens")
    output_tokens      = _col("output_tokens")
    cache_read_tokens  = _col("cache_read_tokens")
    cache_write_tokens = _col("cache_write_tokens")
    split_total = (input_tokens + output_tokens
                   + cache_read_tokens + cache_write_tokens)

    if split_total <= 0:
        usd = estimate_cost_usd(tokens)
        return usd, usd_to_cop(usd)

    usd = estimate_cost_usd_breakdown(
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    )
    legacy_remainder = tokens - (input_tokens + output_tokens)
    if legacy_remainder > 0:
        usd += estimate_cost_usd(legacy_remainder)
    return usd, usd_to_cop(usd)


def _to_cop(usd: Decimal) -> Decimal:
    """COP for an already-summed USD total. Lazy import, same as _estimate_row."""
    from app.services.cost_estimator import usd_to_cop  # noqa: PLC0415
    return usd_to_cop(usd)


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
    try:
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    usage_date,
                    SUM(total_tokens)::BIGINT       AS tokens,
                    SUM(input_tokens)::BIGINT       AS input_tokens,
                    SUM(output_tokens)::BIGINT      AS output_tokens,
                    SUM(cache_read_tokens)::BIGINT  AS cache_read_tokens,
                    SUM(cache_write_tokens)::BIGINT AS cache_write_tokens
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
    # Summed from the per-day costs, not re-derived from the token total: a
    # period can mix pre- and post-0094 days, which are priced differently,
    # so only the day-by-day sum adds up to what the period actually cost.
    total_cost_usd = Decimal("0")
    by_day = []
    for row in rows:
        t = int(row["tokens"] or 0)
        total_tokens += t
        cost_usd, _ = _estimate_row(row, t)
        total_cost_usd += cost_usd
        by_day.append({
            "date": str(row["usage_date"]),
            "tokens": t,
            "cost_usd": float(cost_usd),  # JSON boundary
        })

    total_cost_cop = _to_cop(total_cost_usd)
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
        "monthly_price_cop": int,      # 0 for comp/free orgs
        "total_tokens": int,
        "estimated_cost_usd": float,   # JSON boundary
        "estimated_cost_cop": float,   # JSON boundary
        "orders_count": int,
        "margin_cop": float | None,    # monthly_price - cost_cop; None for comp/free
        "margin_pct": float | None,    # (margin / price) * 100; None if price == 0
    }
    """
    try:
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    su.org_id,
                    o.name                             AS org_name,
                    COALESCE(o.plan_code, 'free')      AS plan_code,
                    COALESCE(pl.monthly_price_cop, 0)  AS monthly_price_cop,
                    SUM(su.total_tokens)::BIGINT       AS total_tokens,
                    SUM(su.input_tokens)::BIGINT       AS input_tokens,
                    SUM(su.output_tokens)::BIGINT      AS output_tokens,
                    SUM(su.cache_read_tokens)::BIGINT  AS cache_read_tokens,
                    SUM(su.cache_write_tokens)::BIGINT AS cache_write_tokens,
                    SUM(su.orders_count)::BIGINT       AS orders_count
                FROM subscription_usage su
                LEFT JOIN organizations o  ON o.id = su.org_id
                LEFT JOIN plan_limits    pl ON pl.plan_code = COALESCE(o.plan_code, 'free')
                WHERE su.usage_date BETWEEN $1 AND $2
                GROUP BY su.org_id, o.name, o.plan_code, pl.monthly_price_cop
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
        cost_usd, cost_cop = _estimate_row(row, tokens)
        plan = (row["plan_code"] or "free").lower()
        monthly_price = int(row["monthly_price_cop"] or 0)

        # Margin only meaningful for paying plans (not comp/free)
        if plan not in ("comp", "free") and monthly_price > 0:
            margin_cop = float(Decimal(str(monthly_price)) - cost_cop)  # JSON boundary
            margin_pct = round((margin_cop / monthly_price) * 100, 1)
        else:
            margin_cop = None
            margin_pct = None

        result.append({
            "org_id": row["org_id"],
            "org_name": row["org_name"] or f"Org #{row['org_id']}",
            "plan_code": plan,
            "monthly_price_cop": monthly_price,
            "total_tokens": tokens,
            "estimated_cost_usd": float(cost_usd),  # JSON boundary
            "estimated_cost_cop": float(cost_cop),  # JSON boundary
            "orders_count": int(row["orders_count"] or 0),
            "margin_cop": margin_cop,
            "margin_pct": margin_pct,
        })
    return result


async def db_platform_margin_summary(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Platform-wide revenue vs cost margin for [start_date, end_date].

    Revenue = sum of monthly_price_cop for paying orgs that had spend in the period.
    Cost    = sum of estimated_cost_cop for all orgs.
    Requires bypass_tenant_scope("internal_cost_dashboard") at call site.

    Returns:
    {
        "total_revenue_cop": float,
        "total_cost_cop": float,
        "net_margin_cop": float,
        "margin_pct": float | None,
        "paying_orgs": int,
    }
    """
    rows = await db_per_restaurant_costs(start_date, end_date, limit=500)
    total_revenue = Decimal("0")
    total_cost = Decimal("0")
    paying_orgs = 0

    for r in rows:
        cost_cop = Decimal(str(r["estimated_cost_cop"]))
        total_cost += cost_cop
        plan = r["plan_code"]
        price = r["monthly_price_cop"]
        if plan not in ("comp", "free") and price > 0:
            total_revenue += Decimal(str(price))
            paying_orgs += 1

    net = total_revenue - total_cost
    margin_pct = round(float(net / total_revenue) * 100, 1) if total_revenue > 0 else None

    return {
        "total_revenue_cop": float(total_revenue),  # JSON boundary
        "total_cost_cop": float(total_cost),         # JSON boundary
        "net_margin_cop": float(net),                # JSON boundary
        "margin_pct": margin_pct,
        "paying_orgs": paying_orgs,
    }


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
    try:
        async with tenant_connection() as conn:
            org_row = await conn.fetchrow(
                "SELECT name, COALESCE(plan_code, 'esencial') AS plan FROM organizations WHERE id = $1",
                org_id,
            )
            rows = await conn.fetch(
                """
                SELECT
                    usage_date,
                    COALESCE(total_tokens, 0)       AS tokens,
                    COALESCE(input_tokens, 0)       AS input_tokens,
                    COALESCE(output_tokens, 0)      AS output_tokens,
                    COALESCE(cache_read_tokens, 0)  AS cache_read_tokens,
                    COALESCE(cache_write_tokens, 0) AS cache_write_tokens,
                    COALESCE(orders_count, 0)       AS orders_count
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
    total_usd = Decimal("0")   # summed per day — see db_platform_cost_summary
    for row in rows:
        t = int(row["tokens"] or 0)
        o = int(row["orders_count"] or 0)
        total_tokens += t
        total_orders += o
        cost_usd, _ = _estimate_row(row, t)
        total_usd += cost_usd
        by_day.append({
            "date": str(row["usage_date"]),
            "tokens": t,
            "cost_usd": float(cost_usd),  # JSON boundary
            "orders_count": o,
        })

    total_cop = _to_cop(total_usd)

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
# Plan codes mirror app/services/plans.py (pricing 2026-09-30, migration 0101).
# Esencial has no AI chat; its budget covers the carta import and the like.
_PLAN_DAILY_TOKEN_LIMITS = {
    "free":        5_000,
    "esencial":    50_000,
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
    days = max((end_date - start_date).days + 1, 1)
    try:
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    su.org_id,
                    o.name                              AS org_name,
                    COALESCE(o.plan_code, 'esencial') AS plan_code,
                    SUM(su.total_tokens)::BIGINT        AS total_tokens,
                    SUM(su.input_tokens)::BIGINT        AS input_tokens,
                    SUM(su.output_tokens)::BIGINT       AS output_tokens,
                    SUM(su.cache_read_tokens)::BIGINT   AS cache_read_tokens,
                    SUM(su.cache_write_tokens)::BIGINT  AS cache_write_tokens
                FROM subscription_usage su
                LEFT JOIN organizations o ON o.id = su.org_id
                WHERE su.usage_date BETWEEN $1 AND $2
                GROUP BY su.org_id, o.name, o.plan_code
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
        plan_limit = _PLAN_DAILY_TOKEN_LIMITS.get(plan, _PLAN_DAILY_TOKEN_LIMITS["esencial"])
        if plan_limit <= 0:
            continue

        total = int(row["total_tokens"] or 0)
        avg_daily = total / days
        pct = (avg_daily / plan_limit) * 100.0

        if pct >= threshold_pct:
            cost_usd, _ = _estimate_row(row, total)
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
