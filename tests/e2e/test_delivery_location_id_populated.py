"""
tests/e2e/test_delivery_location_id_populated.py — Guard: orders.location_id is persisted.

Regression guard for the fix in orders_repo.commit_order_transaction + orders.create_order
+ agent_external.execute_external_action that threads location_id from GPS routing into the
orders INSERT.

Test: after a delivery order commits, SELECT location_id FROM orders WHERE id=$1 must:
  - NOT be NULL
  - be one of the valid location_ids for this org (branch_1_id or principal_loc_id)

If this fails, the fix regressed: location_id is being dropped somewhere in the pipeline.

Run:
  pytest tests/e2e/test_delivery_location_id_populated.py -v -m e2e
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    create_admin_token,
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

CUSTOMER_PHONE_RAW = "+573011222333"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# GPS near branch 1 (zona norte Bogotá)
CUSTOMER_LAT = 4.711200
CUSTOMER_LON = -74.072300
CUSTOMER_ADDRESS = "Calle 72 # 10-34, Bogotá"

MENU = {
    "Empanadas": [
        {
            "name": "Empanaditas de Carne (3 uds)",
            "description": "Empanadas fritas rellenas de carne y papa",
            "price": 15000,
            "active": True,
        }
    ]
}


@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=120.0,
        ) as client:
            yield client


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delivery_order_persists_location_id(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Guard: after a GPS-routed delivery order commits, orders.location_id is NOT NULL
    and matches a valid location_id of the seeded org.

    Flow mirrors test_delivery_full_lifecycle (4 turns) but stops after the order
    commits — no admin status transitions needed for this guard.
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E LocationID Guard Restaurant",
        bot_number_raw="+570E2ELOCID",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),   # branch 1 — near customer GPS
            (4.609710, -74.081741),   # branch 2 — far from customer GPS
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    branch_ids = {b["id"] for b in restaurant["branches"]}

    # Collect all valid location_ids: branches + the principal location.
    # The principal location has no whatsapp_number override.
    with bypass_tenant_scope("e2e_locid_guard_collect_locations"):
        async with pool.acquire() as conn:
            loc_rows = await conn.fetch(
                "SELECT id FROM locations WHERE org_id = $1",
                parent_id,
            )
    valid_location_ids = {r["id"] for r in loc_rows}
    assert valid_location_ids, "No locations found for org — seed_restaurant failed"

    await truncate_e2e_data(pool, parent_id)
    for b in restaurant["branches"]:
        await truncate_e2e_data(pool, b["id"])

    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info(
        "e2e.locid_guard.start",
        parent_id=parent_id,
        valid_location_ids=valid_location_ids,
        bot_number=bot_number,
    )

    # Turn 1: delivery intent + item
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Hola, quiero pedir a domicilio empanaditas",
        bot_number=bot_number,
    )

    # Turn 2: text address
    t2_processed = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=f"Mi direccion es {CUSTOMER_ADDRESS}",
        bot_number=bot_number,
    )
    assert t2_processed >= 1, "Turn 2 was not processed"

    # Turn 2b (GPS): send customer GPS location — this is the critical step.
    # Without GPS, geocoding may fail in E2E and routing_context["location_id"] won't be set.
    # We always send GPS here to guarantee branch routing fires, regardless of whether the
    # bot explicitly asks for it. The bot's agent_external.py processes GPS from the cart
    # (saved by inbox_worker when a location message arrives) even after a text address.
    # NOTE: this is a second address-step message — the bot may ask for payment next or
    # re-acknowledge the address. Either way, routing_context["location_id"] gets populated.
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="",
        bot_number=bot_number,
        lat=CUSTOMER_LAT,
        lon=CUSTOMER_LON,
    )

    # Turn 3: payment method — bot should now ask for this (or have already moved to it)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Efectivo",
        bot_number=bot_number,
    )

    # Turn 4: confirmation — if bot already confirmed, this is a no-op for order creation
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="si confirmo",
        bot_number=bot_number,
    )
    assert processed_4 >= 1, "Turn 4 (confirmation) was not processed"

    # Poll for the order to appear (agent commits asynchronously)
    order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_locid_guard_poll_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, org_id, location_id, order_type, status, total
                    FROM orders
                    WHERE phone = $1
                      AND org_id = $2
                      AND order_type = 'domicilio'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    parent_id,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No delivery order found for phone {CUSTOMER_PHONE}, org {parent_id}. "
        "The bot did not commit an order. Check ANTHROPIC_API_KEY and bot routing."
    )

    order_id = order_row["id"]
    order_location_id = order_row["location_id"]

    log.info(
        "e2e.locid_guard.order_found",
        order_id=order_id,
        org_id=order_row["org_id"],
        location_id=order_location_id,
        valid_location_ids=valid_location_ids,
        total=order_row["total"],
    )

    # ── GUARD: location_id must NOT be NULL ───────────────────────────────────
    assert order_location_id is not None, (
        f"orders.location_id is NULL for order {order_id}. "
        "Fix: commit_order_transaction must receive location_id from routing_context "
        "and write it to the INSERT. See agent_external.execute_external_action "
        "and orders.create_order."
    )

    # ── GUARD: location_id must be a valid location for this org ─────────────
    assert order_location_id in valid_location_ids, (
        f"orders.location_id={order_location_id} is not in valid_location_ids={valid_location_ids} "
        f"for org {parent_id}. This is a cross-org contamination or routing bug."
    )

    log.info(
        "e2e.locid_guard.passed",
        order_id=order_id,
        location_id=order_location_id,
        in_valid_set=order_location_id in valid_location_ids,
    )
    print(
        f"\n[OK] location_id guard passed: order {order_id} has location_id={order_location_id} "
        f"(valid set: {valid_location_ids})"
    )
