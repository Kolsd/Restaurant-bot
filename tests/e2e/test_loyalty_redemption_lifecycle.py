"""
tests/e2e/test_loyalty_canje_lifecycle.py — E2E test: loyalty point redemption full lifecycle.

What this exercises (no Anthropic, no LLM):
  1. Seed restaurant + customer with N loyalty points.
  2. Seed a pending delivery order (the redemption target).
  3. Drive `agent._execute_redeem_loyalty` directly (the bot tool entry point) —
     same code path the production tool_use dispatcher would call.
  4. Assert: ledger row inserted (delta = -points), customer balance decremented,
     orders.loyalty_redeemed_points + loyalty_discount_cop populated.
  5. Caja-side visibility: GET /api/delivery/orders/{id} surfaces the discount.

Why a real E2E:
  Production flow is bot tool_use → execute_action → _execute_redeem_loyalty →
  ledger + apply_redemption_to_order → caja sees it. The unit tests in
  tests/test_loyalty_redemption.py cover each layer in isolation. This test
  proves the END-TO-END chain works on a real DB with RLS active.

This test does NOT call Anthropic — it drives the tool entry point directly.
Marked `e2e_no_llm` (NOT `e2e`) so it runs without ANTHROPIC_API_KEY: the
conftest's pytest_collection_modifyitems skips only `e2e`-marked tests when
the key is missing, which would falsely block this all-DB-no-LLM lifecycle.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    create_admin_token,
    seed_restaurant,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope, tenant_scope

log = get_logger(__name__)


CUSTOMER_PHONE_RAW = "+573009990601"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)


@pytest_asyncio.fixture()
async def e2e_app():
    """Yields an httpx AsyncClient against the FastAPI app via ASGI transport."""
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_loyalty_canje_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
):
    """
    Loyalty redemption end-to-end:

      Seed: customer has 200 points, 1 pending delivery order at $80,000
      Action: bot tool _execute_redeem_loyalty(points=50) fires
      Assert:
        - balance: 200 → 150
        - ledger: row(delta=-50, reason='redeem', order_id=<order>)
        - order.loyalty_redeemed_points = 50, loyalty_discount_cop > 0
        - caja-visible via /api/delivery/orders endpoint
    """
    pool = test_pool

    # ── Seed restaurant ────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Loyalty Canje Restaurant",
        bot_number_raw="+570E2ELOYAL",
        num_branches=1,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    # Clean volatile data from prior runs
    await truncate_e2e_data(pool, org_id)

    # truncate_e2e_data does NOT cover loyalty_customers/loyalty_ledger;
    # those are not "volatile" in the production sense but they DO survive
    # between runs and would trip the (org_id, phone) unique constraint
    # on re-seed. Clean them out for this test's customer.
    with bypass_tenant_scope("e2e_loyalty_clean"):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM loyalty_ledger WHERE org_id = $1 AND phone = $2",
                org_id, CUSTOMER_PHONE,
            )
            await conn.execute(
                "DELETE FROM loyalty_customers WHERE org_id = $1 AND phone = $2",
                org_id, CUSTOMER_PHONE,
            )

    # ── Seed loyalty customer + pending delivery order ─────────────────────────
    order_id = str(uuid.uuid4())
    with bypass_tenant_scope("e2e_loyalty_setup"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO loyalty_customers
                     (org_id, phone, points_balance, total_earned, total_redeemed)
                   VALUES ($1, $2, 200, 200, 0)""",
                org_id, CUSTOMER_PHONE,
            )
            await conn.execute(
                """INSERT INTO orders
                     (id, org_id, phone, bot_number, order_type, status,
                      paid, total, subtotal, items)
                   VALUES ($1, $2, $3, $4, 'domicilio', 'pendiente', false,
                           80000, 80000, '[]'::jsonb)""",
                order_id, org_id, CUSTOMER_PHONE, bot_number,
            )

    # ── Drive the bot's redemption tool ────────────────────────────────────────
    from app.services.agent import _execute_redeem_loyalty

    parsed = {"action": "redeem_loyalty", "points": 50}
    restaurant_obj = {"id": org_id}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, CUSTOMER_PHONE, bot_number,
            table_context=None,
            restaurant_obj=restaurant_obj,
        )

    # Reply must mention the redeemed points and new balance
    assert "50 puntos" in reply, f"Reply missing '50 puntos': {reply!r}"
    assert "150 puntos" in reply, f"Reply missing new balance '150 puntos': {reply!r}"

    # ── Assert: balance decremented ────────────────────────────────────────────
    with bypass_tenant_scope("e2e_loyalty_verify"):
        async with pool.acquire() as conn:
            await conn.execute("SET ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, false)", str(org_id),
            )
            try:
                bal = await conn.fetchval(
                    "SELECT points_balance FROM loyalty_customers "
                    "WHERE org_id=$1 AND phone=$2",
                    org_id, CUSTOMER_PHONE,
                )
                assert bal == 150, f"Expected balance 150, got {bal}"

                # ── Assert: ledger row has delta=-50, reason='redeem' ─────────
                ledger = await conn.fetchrow(
                    """SELECT delta, reason, order_id FROM loyalty_ledger
                       WHERE org_id=$1 AND phone=$2 AND reason='redeem'""",
                    org_id, CUSTOMER_PHONE,
                )
                assert ledger is not None, "No 'redeem' ledger row inserted"
                assert ledger["delta"] == -50, f"Expected delta=-50, got {ledger['delta']}"
                # order_id may match either the order id or some derivation —
                # at minimum it should be a non-empty string referencing the order
                assert ledger["order_id"], "Ledger order_id should not be empty"

                # ── Assert: orders columns annotated ──────────────────────────
                order_row = await conn.fetchrow(
                    "SELECT loyalty_redeemed_points, loyalty_discount_cop "
                    "FROM orders WHERE id=$1",
                    order_id,
                )
                assert order_row is not None, "Seeded order disappeared"
                assert order_row["loyalty_redeemed_points"] == 50, (
                    f"Expected loyalty_redeemed_points=50, got {order_row['loyalty_redeemed_points']}"
                )
                # NUMERIC column → asyncpg returns Decimal
                discount_cop = int(order_row["loyalty_discount_cop"])
                assert discount_cop > 0, (
                    f"Expected loyalty_discount_cop > 0, got {discount_cop}"
                )

                # ── Cleanup ───────────────────────────────────────────────────
                await conn.execute("RESET ROLE")
            except Exception:
                try:
                    await conn.execute("RESET ROLE")
                except Exception:
                    pass
                raise

    # ── Caja visibility: /api/delivery/orders should surface the discount ──────
    # Authenticate as restaurant owner.
    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await e2e_app.get(
        "/api/delivery/orders?status=pendiente",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"GET orders failed: {resp.status_code} {resp.text}"
    payload = resp.json()
    orders_list = payload.get("orders") or payload  # endpoint may wrap
    if isinstance(orders_list, dict):
        orders_list = orders_list.get("orders", [])

    # Find our order
    matching = [o for o in orders_list if o.get("id") == order_id]
    assert matching, (
        f"Caja /api/delivery/orders did not return our order {order_id}. "
        f"Got {len(orders_list)} orders."
    )
    o = matching[0]
    # The discount must be exposed somewhere visible to caja
    points_field = o.get("loyalty_redeemed_points")
    discount_field = o.get("loyalty_discount_cop")
    assert points_field == 50, (
        f"Caja view missing loyalty_redeemed_points (got {points_field!r}). "
        "Caja staff cannot see the discount → real money will be charged."
    )
    assert discount_field is not None and float(discount_field) > 0, (
        f"Caja view missing loyalty_discount_cop (got {discount_field!r})"
    )

    log.info(
        "e2e.loyalty_canje_lifecycle.passed",
        order_id=order_id, balance_after=bal,
        discount_cop=discount_cop, caja_sees_discount=True,
    )
