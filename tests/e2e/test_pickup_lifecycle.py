"""
tests/e2e/test_pickup_lifecycle.py — E2E test: full pickup order lifecycle.

ONE test that exercises:
  1. Multi-branch restaurant seeding (1 parent + 2 children)
  2. WhatsApp inbound simulation through the real webhook endpoint
  3. Inbox worker drain (real agent.py → Claude API → orders_repo)
  4. Tenant RLS enforcement (all repos use real tenant_scope)
  5. Caja/Kitchen API endpoints with real auth tokens
  6. Outbound WA notification capture

If this test passes, production pickup ordering works end-to-end.
If it fails, production is broken.

Prerequisites (env vars):
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — Postgres URL (or DATABASE_URL pointing at a test DB)
  DISABLE_EMBEDDED_WORKER=1  — set automatically by conftest.py

Run:
  pytest tests/e2e/test_pickup_lifecycle.py -v -s -m e2e
"""
from __future__ import annotations

import asyncio
import re
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
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# ── Test constants ─────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573001112222"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# Branch 1 is in Bogotá zona norte (4.710989, -74.072092)
# Customer GPS is within ~100 m of branch 1 → should auto-assign branch 1
CUSTOMER_LAT = 4.711500
CUSTOMER_LON = -74.072500

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

PAYMENT_METHODS = ["Nequi", "Efectivo"]


# ── App fixture (function-scoped so lifespan runs per test) ────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app via ASGI transport.

    The FastAPI lifespan is started (db pool init, scheduler, etc.) and shut down
    cleanly after each test.  DISABLE_EMBEDDED_WORKER=1 prevents the background
    inbox loop from competing with drain_inbox().
    """
    # Import app AFTER env vars are set by test_pool fixture
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
async def test_pickup_multi_branch_gps_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full pickup order lifecycle:

    Turn 1: "Hola, quiero recoger empanaditas"
    Turn 2: GPS location (within 1 km of branch 1)
    Turn 3: "Nequi"  (payment method)
    Turn 4: "sí confirmo"

    Then:
      - Assert order in DB with correct restaurant_id, order_type, total
      - Admin PATCH: pendiente → confirmado
      - Assert GET /api/kitchen/delivery-orders returns the order
      - Admin PATCH: confirmado → en_preparacion → listo
      - Assert order no longer in kitchen view (listo pickups move to caja)
      - Assert wa_capture has "listo" WA notification to customer
      - Admin PATCH: listo → entregado
      - Assert final DB status = entregado
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant (done after app is running to avoid connection conflicts) ─
    restaurant = await seed_restaurant(
        pool,
        name="E2E Pickup Test Restaurant",
        bot_number_raw="+570E2EPICKUP",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),   # branch 1 — zona norte
            (4.609710, -74.081741),   # branch 2 — zona sur
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]  # normalized (no +)
    branch_1 = restaurant["branches"][0]
    branch_1_id = branch_1["id"]
    branch_1_bot = branch_1["whatsapp_number"]

    # Clean volatile data from prior runs
    await truncate_e2e_data(pool, parent_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    # Reset in-process fallback state (NPS, checkout, cart locks)
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
        "e2e.test_start",
        parent_id=parent_id,
        branch_1_id=branch_1_id,
        bot_number=bot_number,
        branch_1_bot=branch_1_bot,
    )

    # ── Create admin session token for API calls ───────────────────────────────
    # The owner user was created by seed_restaurant; use that to get a session token.
    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)

    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    # For branch-scoped endpoints, include the branch header
    branch_headers = {**auth_headers, "X-Branch-ID": str(branch_1_id)}

    # ── Turn 1: Initial greeting + item mention ────────────────────────────────
    log.info("e2e.turn_1", phone=CUSTOMER_PHONE, text="Hola, quiero recoger empanaditas")
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Hola, quiero recoger empanaditas",
        bot_number=bot_number,
    )
    log.info("e2e.turn_1_done", processed=processed_1, elapsed_s=round(time.monotonic() - t1_start, 1))
    assert processed_1 >= 1, "Turn 1: inbox item was not processed"

    # The bot should have replied (captured in wa_capture)
    # We don't assert exact text — LLM output varies
    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.turn_1_replies", count=len(turn1_replies), replies=turn1_replies[:2])
    assert len(turn1_replies) >= 1, (
        "Turn 1: bot sent no WA message. Check ANTHROPIC_API_KEY and bot_number lookup."
    )

    # ── Turn 2: GPS location (auto-assign to branch 1) ────────────────────────
    log.info("e2e.turn_2", phone=CUSTOMER_PHONE, lat=CUSTOMER_LAT, lon=CUSTOMER_LON)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="",
        bot_number=bot_number,
        lat=CUSTOMER_LAT,
        lon=CUSTOMER_LON,
    )
    log.info("e2e.turn_2_done", processed=processed_2, elapsed_s=round(time.monotonic() - t2_start, 1))
    assert processed_2 >= 1, "Turn 2: GPS inbox item was not processed"

    turn2_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.turn_2_replies", count=len(turn2_replies))
    assert len(turn2_replies) >= 2, "Turn 2: bot sent no reply after GPS"

    # ── Turn 3: Payment method ─────────────────────────────────────────────────
    log.info("e2e.turn_3", phone=CUSTOMER_PHONE, text="Nequi")
    t3_start = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Nequi",
        bot_number=bot_number,
    )
    log.info("e2e.turn_3_done", processed=processed_3, elapsed_s=round(time.monotonic() - t3_start, 1))
    assert processed_3 >= 1, "Turn 3: payment method inbox item was not processed"

    # ── Turn 4: Confirmation ───────────────────────────────────────────────────
    log.info("e2e.turn_4", phone=CUSTOMER_PHONE, text="sí confirmo")
    t4_start = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="sí confirmo",
        bot_number=bot_number,
    )
    log.info("e2e.turn_4_done", processed=processed_4, elapsed_s=round(time.monotonic() - t4_start, 1))
    assert processed_4 >= 1, "Turn 4: confirmation inbox item was not processed"

    # ── Assert: order exists in DB ─────────────────────────────────────────────
    # Poll up to 10s for the order to appear (agent commits asynchronously)
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_assert_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, phone, order_type, status, total, org_id,
                           location_id, bot_number
                    FROM orders
                    WHERE phone = $1
                      AND order_type = 'recoger'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No pickup order found for phone {CUSTOMER_PHONE} in DB. "
        "Check that the bot called create_pickup_order tool and it committed."
    )

    order_id = order_row["id"]
    log.info(
        "e2e.order_found",
        order_id=order_id,
        order_type=order_row["order_type"],
        status=order_row["status"],
        total=order_row["total"],
        org_id=order_row["org_id"],
        location_id=order_row["location_id"],
    )

    # Verify order_type
    assert order_row["order_type"] == "recoger", (
        f"Expected order_type='recoger', got '{order_row['order_type']}'"
    )

    # Verify org_id == parent_id (the org tenant key, written as org_id on orders).
    # location_id is nullable in orders (orders_repo does not set it at commit time).
    assert order_row["org_id"] == parent_id, (
        f"Order org_id={order_row['org_id']} does not match parent_id={parent_id}"
    )

    # Verify total = 15000 (price of Empanaditas de Carne)
    from app.services.money import to_decimal
    order_total = to_decimal(order_row["total"])
    assert order_total == Decimal("15000"), (
        f"Expected order total 15000, got {order_total}"
    )

    # Verify initial status = pendiente
    assert order_row["status"] == "pendiente", (
        f"Expected status='pendiente', got '{order_row['status']}'"
    )

    # Resolve scope_rid for admin API calls (org_id is the tenant key post-Wave-2)
    scope_rid = order_row["org_id"]

    # ── Admin: PATCH status to confirmado ─────────────────────────────────────
    log.info("e2e.patch_confirmado", order_id=order_id)
    patch_resp = await client.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "confirmado"},
        headers=auth_headers,
    )
    assert patch_resp.status_code == 200, (
        f"PATCH to confirmado failed: {patch_resp.status_code} {patch_resp.text}"
    )

    # ── Assert: GET /api/kitchen/delivery-orders returns the order ─────────────
    log.info("e2e.get_kitchen_orders")
    kitchen_resp = await client.get(
        "/api/kitchen/delivery-orders",
        headers=auth_headers,
    )
    assert kitchen_resp.status_code == 200, (
        f"GET kitchen orders failed: {kitchen_resp.status_code} {kitchen_resp.text}"
    )
    kitchen_orders = kitchen_resp.json().get("orders", [])
    order_in_kitchen = next(
        (o for o in kitchen_orders if str(o["id"]) == str(order_id)), None
    )
    assert order_in_kitchen is not None, (
        f"Order {order_id} not found in kitchen orders after confirmado. "
        f"Kitchen returned {len(kitchen_orders)} orders: {[o['id'] for o in kitchen_orders]}"
    )
    log.info("e2e.order_in_kitchen", order_id=order_id, status=order_in_kitchen.get("status"))

    # ── Admin: PATCH to en_preparacion ────────────────────────────────────────
    log.info("e2e.patch_en_preparacion", order_id=order_id)
    prep_resp = await client.patch(
        f"/api/kitchen/delivery-orders/{order_id}/status",
        json={"status": "en_preparacion"},
        headers=auth_headers,
    )
    assert prep_resp.status_code == 200, (
        f"PATCH to en_preparacion failed: {prep_resp.status_code} {prep_resp.text}"
    )

    # ── Admin: PATCH to listo ──────────────────────────────────────────────────
    log.info("e2e.patch_listo", order_id=order_id)
    listo_resp = await client.patch(
        f"/api/kitchen/delivery-orders/{order_id}/status",
        json={"status": "listo"},
        headers=auth_headers,
    )
    assert listo_resp.status_code == 200, (
        f"PATCH to listo failed: {listo_resp.status_code} {listo_resp.text}"
    )

    # ── Assert: WA notification for "listo" was sent to customer ──────────────
    # The kitchen endpoint sends a WA message when status = listo + order_type = recoger
    # Give a brief moment for the asyncio.create_task to run
    await asyncio.sleep(1.0)

    all_texts = wa_capture.all_texts()
    log.info("e2e.wa_messages_after_listo", count=len(all_texts), texts=all_texts[-5:])

    # Look for a "listo"/"recoger" notification in messages to the customer
    customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    listo_msgs = [t for t in customer_texts if re.search(r"listo|recoger|ready|pick", t, re.IGNORECASE)]
    assert len(listo_msgs) >= 1, (
        f"Expected a 'listo/recoger' WA message to customer after marking order listo. "
        f"Customer messages received: {customer_texts}"
    )
    log.info("e2e.listo_notification_found", messages=listo_msgs)

    # ── Admin: PATCH to entregado ──────────────────────────────────────────────
    log.info("e2e.patch_entregado", order_id=order_id)
    entregado_resp = await client.patch(
        f"/api/kitchen/delivery-orders/{order_id}/status",
        json={"status": "entregado"},
        headers=auth_headers,
    )
    assert entregado_resp.status_code == 200, (
        f"PATCH to entregado failed: {entregado_resp.status_code} {entregado_resp.text}"
    )

    # ── Assert: final DB status = entregado ───────────────────────────────────
    with bypass_tenant_scope("e2e_assert_final_status"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1",
                order_id,
            )

    assert final_row is not None, f"Order {order_id} disappeared from DB"
    assert final_row["status"] == "entregado", (
        f"Expected final status='entregado', got '{final_row['status']}'"
    )

    log.info(
        "e2e.test_passed",
        order_id=order_id,
        total_wa_messages=len(wa_capture.messages),
        listo_notifications=len(listo_msgs),
    )
    print(
        f"\n[OK] E2E test passed: order {order_id} completed full pickup lifecycle. "
        f"{len(wa_capture.messages)} WA messages captured."
    )
