"""
tests/e2e/test_subscription_enforcement_lifecycle.py — E2E test: monthly order cap.

What this exercises (no Anthropic, no LLM):
  1. Seed a restaurant with subscription_plan='free' (cap=50 monthly orders)
     and override the cap to 3 via features.plan_limits for fast assertions.
  2. Seed 2 orders directly via subscription_repo.db_increment_orders to
     simulate prior month-to-date consumption.
  3. Assert enforce_order_limit returns OK while under cap.
  4. Increment to 3 (= cap), then assert enforce_order_limit raises
     UsageLimitExceeded — the bot would refuse the 4th order.
  5. Verify db_get_usage_summary returns the expected aggregate.
  6. Cross-tenant: a sibling org's counter is independent (RLS isolation).

Why a real E2E:
  This validates the full chain — RLS GUC, subscription_usage UPSERT,
  monthly aggregation across rows, plan_limits override resolution — against
  a real Postgres with the migrated schema. Unit tests in
  test_subscription_guard.py mock the pool; this hits real RLS + real SQL.

Marked `e2e_no_llm` so it runs without ANTHROPIC_API_KEY (DB-only test).
"""
from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from datetime import date, timedelta, datetime, timezone

from tests.e2e.conftest import seed_restaurant, truncate_e2e_data
from app.services.tenant_context import bypass_tenant_scope, tenant_scope
from app.repositories import subscription_repo
from app.services.subscription_guard import enforce_order_limit
from app.services.database import UsageLimitExceeded


@pytest_asyncio.fixture()
async def two_orgs(test_pool: asyncpg.Pool):
    """Seed two isolated restaurants and return (org_a_id, org_b_id)."""
    rest_a = await seed_restaurant(
        test_pool,
        name="E2E Sub Limit Org A",
        bot_number_raw="+570SUBE2EA1",
        num_branches=1,
    )
    rest_b = await seed_restaurant(
        test_pool,
        name="E2E Sub Limit Org B",
        bot_number_raw="+570SUBE2EB1",
        num_branches=1,
    )
    await truncate_e2e_data(test_pool, rest_a["id"])
    await truncate_e2e_data(test_pool, rest_b["id"])

    # Clean any subscription_usage rows for either org from prior test runs
    with bypass_tenant_scope("e2e_sub_clean"):
        async with test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM subscription_usage WHERE org_id IN ($1, $2)",
                rest_a["id"], rest_b["id"],
            )

    return rest_a["id"], rest_b["id"]


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_subscription_order_cap_enforcement_lifecycle(
    test_pool: asyncpg.Pool,
    two_orgs,
):
    """
    Subscription monthly-order cap end-to-end:

      Setup: Org A on plan 'basic' with features.plan_limits.monthly_orders=3.
      Action: Increment orders 0 → 1 → 2 → 3, asserting guard at each step.
      Assert: enforce_order_limit raises only at cap (used >= cap),
              counter increments are atomic and observed by guard,
              cross-tenant isolation: Org B counter unchanged.
    """
    pool = test_pool
    org_a, org_b = two_orgs

    rest_a = {
        "subscription_plan": "basic",
        "features": {"plan_limits": {"monthly_orders": 3}},
    }
    rest_b = {
        "subscription_plan": "basic",
        "features": {"plan_limits": {"monthly_orders": 3}},
    }

    # ── Phase 1: empty tenant → guard passes (used=0 < cap=3) ────────────────
    with tenant_scope(org_a):
        await enforce_order_limit(org_a, rest_a)  # MUST NOT raise
        summary = await subscription_repo.db_get_usage_summary(org_a)
    assert summary["month"]["orders_count"] == 0

    # ── Phase 2: increment 1 → guard still passes ────────────────────────────
    with tenant_scope(org_a):
        new_total = await subscription_repo.db_increment_orders(org_a)
        assert new_total == 1, f"Expected monthly total=1 after first increment, got {new_total}"

        await enforce_order_limit(org_a, rest_a)  # 1 < 3 — OK

        # Phase 3: 2 → still under cap
        new_total = await subscription_repo.db_increment_orders(org_a)
        assert new_total == 2

        await enforce_order_limit(org_a, rest_a)  # 2 < 3 — OK

        # Phase 4: 3 → AT cap → guard MUST raise (>= semantics)
        new_total = await subscription_repo.db_increment_orders(org_a)
        assert new_total == 3

        with pytest.raises(UsageLimitExceeded) as exc:
            await enforce_order_limit(org_a, rest_a)
        assert exc.value.resource == "órdenes"
        assert exc.value.used == 3
        assert exc.value.limit == 3

    # ── Phase 5: cross-tenant isolation ──────────────────────────────────────
    # Org B is untouched — its counter is 0, its guard passes.
    with tenant_scope(org_b):
        summary_b = await subscription_repo.db_get_usage_summary(org_b)
        assert summary_b["month"]["orders_count"] == 0, (
            f"Org B counter polluted by Org A: {summary_b}"
        )
        # Guard passes — Org B still has full quota
        await enforce_order_limit(org_b, rest_b)

    # ── Phase 6: re-read Org A summary stays at 3 (not 4) ────────────────────
    with tenant_scope(org_a):
        summary_a = await subscription_repo.db_get_usage_summary(org_a)
    assert summary_a["month"]["orders_count"] == 3
    assert summary_a["today"]["orders_count"] == 3


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_subscription_monthly_aggregation_spans_days(
    test_pool: asyncpg.Pool,
    two_orgs,
):
    """
    Verify monthly aggregation sums across multiple days within the same month.

    Seed 3 distinct same-month days each with orders_count, then assert
    db_get_usage_summary aggregates them correctly.
    """
    pool = test_pool
    org_a, _ = two_orgs

    today = datetime.now(timezone.utc).date()
    if today.day < 3:
        pytest.skip("Test requires day-of-month >= 3")

    # Seed 3 days × 5 orders within the current month (rows persist via direct insert
    # so we don't rely on db_increment_orders's CURRENT_DATE behavior).
    with bypass_tenant_scope("e2e_sub_agg_seed"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_a),
            )
            for offset in range(3):
                d = today - timedelta(days=offset)
                await conn.execute(
                    """INSERT INTO subscription_usage (org_id, usage_date, orders_count)
                       VALUES ($1, $2, 5)
                       ON CONFLICT (org_id, usage_date) DO UPDATE
                          SET orders_count = subscription_usage.orders_count + 5""",
                    org_a, d,
                )

    with tenant_scope(org_a):
        summary = await subscription_repo.db_get_usage_summary(org_a)

    # 3 days × 5 = 15 orders for the month. Today's row shows 5.
    assert summary["month"]["orders_count"] == 15, (
        f"Expected 15 (3×5) monthly, got {summary['month']['orders_count']}"
    )
    assert summary["today"]["orders_count"] == 5, (
        f"Expected today=5, got {summary['today']['orders_count']}"
    )

    # Now bump the cap such that 15 trips the limit at cap=15
    rest_a = {
        "subscription_plan": "basic",
        "features": {"plan_limits": {"monthly_orders": 15}},
    }
    with tenant_scope(org_a):
        with pytest.raises(UsageLimitExceeded) as exc:
            await enforce_order_limit(org_a, rest_a)
    assert exc.value.used == 15
    assert exc.value.limit == 15
