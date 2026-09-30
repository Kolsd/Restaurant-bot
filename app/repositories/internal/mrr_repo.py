"""
MRR repository — Monthly Recurring Revenue analytics.

Cross-tenant, GLOBAL — must be called under bypass_tenant_scope.
Uses _get_pool() directly (same pattern as crm_repo.py for internal tools).
"""

from __future__ import annotations

from app.services import plans
from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessor — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


# One row per org with what it pays: plan, frozen founder price, trial/comp
# window and how many active sedes it has (every org has at least its
# primary one, so an org with no location row still counts one sede).
_ORG_BILLING_SQL = """
    SELECT o.id, o.plan_code, o.founder_price_cop, o.created_at,
           (o.comp_until IS NOT NULL AND o.comp_until > NOW()) AS comp,
           GREATEST(1, (SELECT COUNT(*) FROM locations l
                         WHERE l.org_id = o.id AND l.active))::int AS sedes
    FROM organizations o
"""


def _org_mrr(row) -> int:
    return plans.monthly_price_per_sede(row["plan_code"], row["founder_price_cop"]) * row["sedes"]


async def db_compute_mrr() -> dict:
    """
    Compute current MRR + breakdown. GLOBAL — must be called under bypass_tenant_scope.

    Pricing is per sede (docs/claude/status.md #15): an org pays its plan's
    price — or its frozen founder price — times its active sedes. Orgs in
    their free days (comp_until in the future) are not billed.

    Returns:
        {
            "mrr_total_cop": int,
            "by_plan": [
                {"plan_code": "esencial", "monthly_price_cop": 119000,
                 "paying_count": N, "sedes": S, "mrr_cop": ...},
                ...
            ],
            "paying_count": int,   # orgs currently billed
            "comp_count": int,     # orgs in trial / comp (not billed)
            "free_count": int,     # orgs on no known plan
            "total_orgs": int,
        }
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_ORG_BILLING_SQL)

    by_plan = {
        code: {"plan_code": code, "monthly_price_cop": plans.PRICES_COP[code],
               "paying_count": 0, "sedes": 0, "mrr_cop": 0}
        for code in plans.PLAN_ORDER
    }
    comp_count = free_count = 0
    for row in rows:
        if row["comp"]:
            comp_count += 1
        elif row["plan_code"] in plans.PAYING_PLANS:
            item = by_plan[row["plan_code"]]
            item["paying_count"] += 1
            item["sedes"] += row["sedes"]
            item["mrr_cop"] += _org_mrr(row)
        else:
            free_count += 1

    items = list(by_plan.values())
    paying_count = sum(i["paying_count"] for i in items)
    return {
        "mrr_total_cop": sum(i["mrr_cop"] for i in items),
        "by_plan": items,
        "paying_count": paying_count,
        "comp_count": comp_count,
        "free_count": free_count,
        "total_orgs": len(rows),
    }


async def db_compute_mrr_delta() -> dict:
    """
    Approximate last-month MRR for MoM delta comparison.

    Heuristic: orgs created before the start of the current month that are
    billed today, at today's plan, price and sede count. Directionally right
    for the founder's board update without a snapshot table.

    Returns:
        {
            "mrr_last_month_cop": int,
            "delta_cop": int,
            "delta_pct": float,   # positive = growth, e.g. 12.5 means +12.5%
        }
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _ORG_BILLING_SQL + " WHERE o.created_at < date_trunc('month', NOW())"
        )
    mrr_last_month_cop = sum(
        _org_mrr(r) for r in rows
        if not r["comp"] and r["plan_code"] in plans.PAYING_PLANS
    )

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
