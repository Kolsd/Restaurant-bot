"""
MRR repository — Monthly Recurring Revenue analytics.

Cross-tenant, GLOBAL — must be called under bypass_tenant_scope.
Uses _get_pool() directly (same pattern as crm_repo.py for internal tools).
"""

from __future__ import annotations

from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessor — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


# Plans that constitute "paying" (not comp or free)
_PAYING_PLAN_CODES = {"pulso", "restaurante", "pro", "cadena"}


async def db_compute_mrr() -> dict:
    """
    Compute current MRR + breakdown. GLOBAL — must be called under bypass_tenant_scope.

    Returns:
        {
            "mrr_total_cop": int,
            "by_plan": [
                {"plan_code": "pulso", "monthly_price_cop": 149000, "paying_count": N, "mrr_cop": N*149000},
                ...
            ],
            "paying_count": int,   # orgs currently billed
            "comp_count": int,     # orgs on comp (not billed)
            "free_count": int,     # free / demo orgs (plan_code IS NULL or 'free')
            "total_orgs": int,
        }
    """
    pool = await _get_pool()

    # Fetch plan prices
    async with pool.acquire() as conn:
        price_rows = await conn.fetch(
            "SELECT plan_code, monthly_price_cop FROM plan_limits"
        )
    plan_prices: dict[str, int] = {r["plan_code"]: r["monthly_price_cop"] for r in price_rows}

    # Fetch org status aggregated
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH org_status AS (
                SELECT
                    o.id,
                    o.plan_code,
                    o.comp_until,
                    CASE
                        WHEN o.comp_until IS NOT NULL AND o.comp_until > NOW() THEN 'comp'
                        WHEN o.plan_code IS NULL OR o.plan_code IN ('free', '') THEN 'free'
                        WHEN o.plan_code IN ('pulso', 'restaurante', 'pro', 'cadena') THEN 'paying'
                        ELSE 'free'
                    END AS status
                FROM organizations o
            )
            SELECT plan_code, status, COUNT(*)::int AS cnt
            FROM org_status
            GROUP BY plan_code, status
            """
        )

    # Aggregate
    paying_count = 0
    comp_count = 0
    free_count = 0
    total_orgs = 0
    # plan_code -> paying_count
    by_plan_counts: dict[str, int] = {}

    for row in rows:
        cnt = row["cnt"]
        status = row["status"]
        plan_code = row["plan_code"] or "free"
        total_orgs += cnt

        if status == "paying":
            paying_count += cnt
            by_plan_counts[plan_code] = by_plan_counts.get(plan_code, 0) + cnt
        elif status == "comp":
            comp_count += cnt
        else:
            free_count += cnt

    # Build by_plan list (only for paying plans, in canonical order)
    canonical_order = ["pulso", "restaurante", "pro", "cadena"]
    by_plan = []
    for pc in canonical_order:
        price = plan_prices.get(pc, 0)
        cnt = by_plan_counts.get(pc, 0)
        by_plan.append({
            "plan_code": pc,
            "monthly_price_cop": price,
            "paying_count": cnt,
            "mrr_cop": price * cnt,
        })

    mrr_total_cop = sum(item["mrr_cop"] for item in by_plan)

    return {
        "mrr_total_cop": mrr_total_cop,
        "by_plan": by_plan,
        "paying_count": paying_count,
        "comp_count": comp_count,
        "free_count": free_count,
        "total_orgs": total_orgs,
    }


async def db_compute_mrr_delta() -> dict:
    """
    Approximate last-month MRR for MoM delta comparison.

    Heuristic for v1: count orgs created BEFORE the start of the current month
    that are currently on a paying plan (comp_until check excluded — we approximate
    last month as "same plan, same comp status"). This is directionally correct
    for the founder's board update and avoids needing a snapshot table.

    Returns:
        {
            "mrr_last_month_cop": int,
            "delta_cop": int,
            "delta_pct": float,   # positive = growth, e.g. 12.5 means +12.5%
        }
    """
    pool = await _get_pool()

    # Fetch plan prices
    async with pool.acquire() as conn:
        price_rows = await conn.fetch(
            "SELECT plan_code, monthly_price_cop FROM plan_limits"
        )
    plan_prices: dict[str, int] = {r["plan_code"]: r["monthly_price_cop"] for r in price_rows}

    # Count paying orgs created before the start of the current month
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                o.plan_code,
                COUNT(*)::int AS cnt
            FROM organizations o
            WHERE
                o.plan_code IN ('pulso', 'restaurante', 'pro', 'cadena')
                AND (o.comp_until IS NULL OR o.comp_until <= NOW())
                AND o.created_at < date_trunc('month', NOW())
            GROUP BY o.plan_code
            """
        )

    mrr_last_month_cop = sum(
        plan_prices.get(r["plan_code"], 0) * r["cnt"]
        for r in rows
    )

    # Get current MRR for delta calculation
    current = await db_compute_mrr()
    current_mrr = current["mrr_total_cop"]

    delta_cop = current_mrr - mrr_last_month_cop
    if mrr_last_month_cop > 0:
        delta_pct = round((delta_cop / mrr_last_month_cop) * 100, 1)
    else:
        delta_pct = 0.0 if current_mrr == 0 else 100.0

    return {
        "mrr_last_month_cop": mrr_last_month_cop,
        "delta_cop": delta_cop,
        "delta_pct": delta_pct,
    }
