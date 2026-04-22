"""
tests/e2e/test_delivery_fee_computed.py — E2E: delivery_fee added to order total.

ONE test that exercises:
  1. Restaurant seeding with features.delivery_fee = 5000
  2. Full 4-turn delivery flow (item → address → payment → confirm)
  3. Assert: orders.subtotal == 15000 (empanada only)
  4. Assert: orders.delivery_fee == 5000 (from features config)
  5. Assert: orders.total == 20000 (subtotal + fee, Decimal exact)

Why this test:
  orders.py:create_order reads features.delivery_fee from the restaurant config
  and computes total = subtotal + delivery_fee. The value is stored as NUMERIC(14,2)
  in the orders table. This test verifies the end-to-end path from feature flag to
  persisted Decimal.

Feature key (from orders.py:258):
  _raw_feats.get("delivery_fee", 0)  — when order_type == "domicilio"

The delivery_fee column (orders table, NUMERIC(14,2)) was added in migration 0046
(money precision hardening). It defaults to 0 for restaurants without the flag.

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — Postgres URL (dedicated test DB with alembic head applied)

Run:
  pytest tests/e2e/test_delivery_fee_computed.py -v -s -m e2e
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
    drain_inbox,
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.money import to_decimal
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# ── Test constants ─────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573009995566"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# Branch near the customer so GPS routing succeeds
CUSTOMER_LAT = 4.711200
CUSTOMER_LON = -74.072300
CUSTOMER_ADDRESS = "Carrera 15 # 93-47, apto 501, Bogota"

EMPANADA_PRICE = 15000
DELIVERY_FEE = 5000

MENU = {
    "Empanadas": [
        {
            "name": "Empanaditas de Carne (3 uds)",
            "description": "Empanadas fritas rellenas de carne y papa",
            "price": EMPANADA_PRICE,
            "active": True,
        }
    ]
}

PAYMENT_METHODS = ["Nequi", "Efectivo"]


# ── App fixture ────────────────────────────────────────────────────────────────

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


# ── The single E2E test ────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delivery_fee_computed(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Delivery order with features.delivery_fee=5000:
      subtotal=15000, delivery_fee=5000, total=20000 (all Decimal-exact in DB).
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant with delivery_fee configured ───────────────────────────
    # Two branch locations so the GPS routing in agent_external.py finds a branch.
    restaurant = await seed_restaurant(
        pool,
        name="E2E Delivery Fee Test Restaurant",
        bot_number_raw="+570E2EDELIVFEE",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),   # branch 1 — zona norte (near customer)
            (4.609710, -74.081741),   # branch 2 — zona sur
        ],
        features_override={
            "delivery_fee": DELIVERY_FEE,   # key read by orders.py:258
        },
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, org_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

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
        "e2e.delivery_fee.test_start",
        org_id=org_id,
        bot_number=bot_number,
        delivery_fee=DELIVERY_FEE,
    )

    # ── Turn 1: item + delivery intent ────────────────────────────────────────
    turn1_text = "Hola, quiero pedir a domicilio unas empanaditas de carne"
    log.info("e2e.delivery_fee.turn_1", text=turn1_text)
    t1 = time.monotonic()
    proc1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn1_text, bot_number=bot_number,
    )
    log.info("e2e.delivery_fee.turn_1_done", processed=proc1, elapsed=round(time.monotonic()-t1, 1))
    assert proc1 >= 1, "Turn 1 not processed"

    replies1 = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(replies1) >= 1, (
        "Turn 1: bot sent no WA message. Check ANTHROPIC_API_KEY and bot_number lookup."
    )
    log.info("e2e.delivery_fee.turn_1_reply", preview=replies1[-1][:80])

    # ── Turn 2: text address ──────────────────────────────────────────────────
    turn2_text = f"Mi dirección es {CUSTOMER_ADDRESS}"
    log.info("e2e.delivery_fee.turn_2", text=turn2_text)
    t2 = time.monotonic()
    proc2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn2_text, bot_number=bot_number,
    )
    log.info("e2e.delivery_fee.turn_2_done", processed=proc2, elapsed=round(time.monotonic()-t2, 1))
    assert proc2 >= 1, "Turn 2 not processed"

    wa_count_after_t2 = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))

    # ── Turn 2b (optional): GPS if bot still asks for location ────────────────
    # Some LLM responses ask for GPS even after a text address. We send location
    # proactively to unblock the flow.
    replies_after_t2 = wa_capture.texts_to(CUSTOMER_PHONE_RAW)[wa_count_after_t2-1:]
    combined_t2 = " ".join(replies_after_t2).lower()
    if "ubicación" in combined_t2 or "ubicacion" in combined_t2 or "gps" in combined_t2 or "localización" in combined_t2:
        log.info("e2e.delivery_fee.turn_2b_gps_needed")
        proc2b = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW, text="", bot_number=bot_number,
            lat=CUSTOMER_LAT, lon=CUSTOMER_LON,
        )
        assert proc2b >= 1, "Turn 2b (GPS) not processed"

    # ── Turn 3: payment method ────────────────────────────────────────────────
    turn3_text = "Efectivo"
    log.info("e2e.delivery_fee.turn_3", text=turn3_text)
    t3 = time.monotonic()
    proc3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn3_text, bot_number=bot_number,
    )
    log.info("e2e.delivery_fee.turn_3_done", processed=proc3, elapsed=round(time.monotonic()-t3, 1))
    assert proc3 >= 1, "Turn 3 not processed"

    # ── Turn 4: confirmation ──────────────────────────────────────────────────
    turn4_text = "Sí, confirmo el pedido"
    log.info("e2e.delivery_fee.turn_4", text=turn4_text)
    t4 = time.monotonic()
    proc4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn4_text, bot_number=bot_number,
    )
    log.info("e2e.delivery_fee.turn_4_done", processed=proc4, elapsed=round(time.monotonic()-t4, 1))
    assert proc4 >= 1, "Turn 4 not processed"

    # ── Find the created order in DB ──────────────────────────────────────────
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_delivery_fee_assert_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, subtotal, delivery_fee, total, order_type, status, org_id
                    FROM orders
                    WHERE org_id = $1
                      AND phone = $2
                      AND order_type = 'domicilio'
                      AND (base_order_id IS NULL OR base_order_id = id)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    org_id,
                    CUSTOMER_PHONE,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No delivery order found for phone={CUSTOMER_PHONE} org_id={org_id}. "
        "The confirmation turn may have failed, or the address was not accepted. "
        f"WA messages captured: {wa_capture.texts_to(CUSTOMER_PHONE_RAW)}"
    )
    log.info(
        "e2e.delivery_fee.order_found",
        id=order_row["id"],
        subtotal=str(order_row["subtotal"]),
        delivery_fee=str(order_row["delivery_fee"]),
        total=str(order_row["total"]),
        status=order_row["status"],
    )

    # ── Assert: subtotal == 15000 ─────────────────────────────────────────────
    # asyncpg returns NUMERIC as Decimal natively.
    db_subtotal = to_decimal(order_row["subtotal"])
    assert db_subtotal == Decimal("15000"), (
        f"orders.subtotal expected Decimal('15000'), got {db_subtotal!r}. "
        "The empanada price or cart subtotal computation may be wrong."
    )

    # ── Assert: delivery_fee == 5000 ──────────────────────────────────────────
    db_delivery_fee = to_decimal(order_row["delivery_fee"])
    assert db_delivery_fee == Decimal("5000"), (
        f"orders.delivery_fee expected Decimal('5000'), got {db_delivery_fee!r}. "
        "The delivery_fee feature key may be misread from features config "
        "(orders.py:258 reads _raw_feats.get('delivery_fee', 0)). "
        f"Check that features_override={'delivery_fee': {DELIVERY_FEE}} was persisted."
    )

    # ── Assert: total == 20000 (subtotal + delivery_fee) ─────────────────────
    db_total = to_decimal(order_row["total"])
    assert db_total == Decimal("20000"), (
        f"orders.total expected Decimal('20000'), got {db_total!r}. "
        f"subtotal={db_subtotal}, delivery_fee={db_delivery_fee}. "
        "The total computation in orders.py may not be adding the delivery_fee."
    )

    # ── Assert: order_type is domicilio ───────────────────────────────────────
    assert order_row["order_type"] == "domicilio", (
        f"order_type expected 'domicilio', got '{order_row['order_type']}'"
    )

    log.info(
        "e2e.delivery_fee.test_passed",
        order_id=order_row["id"],
        subtotal=str(db_subtotal),
        delivery_fee=str(db_delivery_fee),
        total=str(db_total),
    )
    print(
        f"\n[OK] Delivery fee test passed: "
        f"order {order_row['id']} — subtotal={db_subtotal}, "
        f"delivery_fee={db_delivery_fee}, total={db_total} (all Decimal-exact). "
        f"{len(wa_capture.messages)} WA messages captured."
    )
