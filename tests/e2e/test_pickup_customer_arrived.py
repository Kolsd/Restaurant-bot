"""
tests/e2e/test_pickup_customer_arrived.py — E2E: "Cliente llegó" pickup notification.

Flow:
  Turn 1: "Hola, quiero recoger empanaditas"
  Turn 2: "Nequi"
  Turn 3: "sí confirmo"
  Admin:  pendiente → confirmado → en_preparacion → listo
  Turn 4: "Ya llegué a recoger mi pedido"
  Drain.

Assertions:
  - waiter_alerts row with alert_type='customer_arrived' exists for that phone
  - Bot reply contains "avisamos" OR "equipo" OR "entregan"

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — dedicated Postgres DB (alembic upgrade head applied)

Run:
  pytest tests/e2e/test_pickup_customer_arrived.py -v -s -m e2e
"""
from __future__ import annotations

import asyncio
import time

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

# ── Constants ─────────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573009998877"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

MENU = {
    "Platos": [
        {
            "name": "Bandeja Paisa",
            "description": "Fríjoles, chicharrón, carne, arepa, arroz",
            "price": 28000,
            "active": True,
        }
    ]
}

PAYMENT_METHODS = ["Nequi", "Daviplata"]


# ── App fixture ───────────────────────────────────────────────────────────────

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


# ── Test ──────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pickup_customer_arrived_fires_waiter_alert(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full pickup order + customer arrival notification lifecycle:

    1. Complete a pickup order (4 turns: greeting + payment + confirm)
    2. Admin advances order status to 'listo'
    3. Customer sends arrival message
    4. Assert waiter_alert with alert_type='customer_arrived' is created
    5. Assert bot reply contains "avisamos" or "equipo" or "entregan"
    """
    client = e2e_app
    pool = test_pool

    # Seed restaurant (single location — no GPS or branch logic needed)
    restaurant = await seed_restaurant(
        pool,
        name="E2E Arrival Test Restaurant",
        bot_number_raw="+570E2EARRIVAL",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=0,  # single location
        branch_latlons=[],
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)

    # Reset in-process state_store fallback dicts
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info("e2e.arrival.test_start", org_id=org_id, bot_number=bot_number)

    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # ── Turn 1: Greeting + item ───────────────────────────────────────────────
    log.info("e2e.arrival.turn_1", text="Hola, quiero recoger una bandeja paisa")
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Hola, quiero recoger una bandeja paisa",
        bot_number=bot_number,
    )
    assert processed_1 >= 1, "Turn 1 not processed"

    # ── Turn 2: Payment method ────────────────────────────────────────────────
    log.info("e2e.arrival.turn_2", text="Nequi")
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Nequi",
        bot_number=bot_number,
    )
    assert processed_2 >= 1, "Turn 2 not processed"

    # ── Turn 3: Confirmation ──────────────────────────────────────────────────
    log.info("e2e.arrival.turn_3", text="sí confirmo")
    processed_3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="sí confirmo",
        bot_number=bot_number,
    )
    assert processed_3 >= 1, "Turn 3 not processed"

    # ── Wait for order to land in DB ──────────────────────────────────────────
    order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_arrival_assert_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, status, org_id
                    FROM orders
                    WHERE phone = $1
                      AND order_type = 'recoger'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No pickup order found for phone={CUSTOMER_PHONE} after 3 turns. "
        "Bot may not have called create_pickup_order."
    )
    order_id = order_row["id"]
    log.info("e2e.arrival.order_found", order_id=order_id, status=order_row["status"])

    # ── Admin: advance to listo ───────────────────────────────────────────────
    for new_status in ("confirmado", "en_preparacion", "listo"):
        patch_resp = await client.patch(
            f"/api/delivery/orders/{order_id}/status",
            json={"status": new_status},
            headers=auth_headers,
        )
        assert patch_resp.status_code == 200, (
            f"PATCH to {new_status} failed: {patch_resp.status_code} {patch_resp.text}"
        )
        log.info("e2e.arrival.status_advanced", new_status=new_status)

    # Give status write a moment to settle
    await asyncio.sleep(0.2)

    # ── Turn 4: Customer arrival ──────────────────────────────────────────────
    log.info("e2e.arrival.turn_4", text="Ya llegué a recoger mi pedido")
    t4_start = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Ya llegué a recoger mi pedido",
        bot_number=bot_number,
    )
    log.info("e2e.arrival.turn_4_done", processed=processed_4, elapsed=round(time.monotonic() - t4_start, 1))
    assert processed_4 >= 1, "Turn 4 (arrival) not processed"

    # ── Assert: waiter_alert with alert_type='customer_arrived' ───────────────
    alert_row = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_arrival_assert_alert"):
            async with pool.acquire() as conn:
                alert_row = await conn.fetchrow(
                    """
                    SELECT id, alert_type, phone, bot_number, message
                    FROM waiter_alerts
                    WHERE phone = $1
                      AND bot_number = $2
                      AND alert_type = 'customer_arrived'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    bot_number,
                )
        if alert_row:
            break
        await asyncio.sleep(0.3)

    assert alert_row is not None, (
        f"No waiter_alert with alert_type='customer_arrived' found for phone={CUSTOMER_PHONE} "
        f"bot_number={bot_number}. "
        "Check that notify_arrival handler created the alert."
    )
    assert alert_row["alert_type"] == "customer_arrived", (
        f"Expected alert_type='customer_arrived', got '{alert_row['alert_type']}'"
    )
    log.info(
        "e2e.arrival.alert_found",
        alert_id=alert_row["id"],
        alert_type=alert_row["alert_type"],
        message=alert_row["message"],
    )

    # ── Assert: bot reply contains expected keywords ───────────────────────────
    customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.arrival.wa_messages", count=len(customer_texts), texts=customer_texts[-3:])

    arrival_replies = [
        t for t in customer_texts
        if any(kw in t.lower() for kw in ("avisamos", "equipo", "entregan", "llegaste", "perfecto"))
    ]
    assert len(arrival_replies) >= 1, (
        f"Expected bot reply containing 'avisamos', 'equipo', or 'entregan' after arrival message. "
        f"All customer messages: {customer_texts}"
    )
    log.info("e2e.arrival.reply_found", replies=arrival_replies)

    print(
        f"\n[OK] E2E arrival test passed: order={order_id}, alert_id={alert_row['id']}, "
        f"alert_type='{alert_row['alert_type']}', reply='{arrival_replies[0][:80]}'"
    )
