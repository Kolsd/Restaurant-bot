"""
tests/e2e/test_nps_lifecycle.py — E2E test: NPS score-save after delivery.

Flow exercised:
  1. Customer places a delivery order (turns 1-4: intent → address → payment → confirm)
  2. Admin PATCHes order to status="entregado"
  3. trigger_nps is awaited synchronously in update_delivery_status (the fix)
     → state_store.nps_set is committed BEFORE PATCH response returns
  4. Customer sends "5" as next inbound message
  5. _try_nps_active_flow intercepts the message, saves nps_responses row, returns reply
  6. Assert: nps_responses row exists with score=5

Root cause of the original bug:
  send_delivery_notification was called via asyncio.create_task, so trigger_nps
  (inside it) ran asynchronously AFTER the PATCH returned. The test's next
  simulate_whatsapp_inbound could reach _try_nps_active_flow before nps_set
  completed, causing nps_get to return None and the score message to fall
  through to the LLM with no DB write.

Fix: trigger_nps is now awaited directly in update_delivery_status before
launching the background WA notification task.

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — dedicated (non-production) Postgres DB

Run:
  pytest tests/e2e/test_nps_lifecycle.py -v -s -m e2e
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
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# ── Test constants ─────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573201112233"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# Customer GPS near branch 1 (Bogotá zona norte)
CUSTOMER_LAT = 4.711200
CUSTOMER_LON = -74.072300
CUSTOMER_ADDRESS = "Carrera 15 # 93-47, apto 501, Bogotá"

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

PAYMENT_METHODS = ["Efectivo", "Nequi"]


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(test_pool, wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app via ASGI transport.

    Depends on test_pool to guarantee DATABASE_URL is set in os.environ
    BEFORE the app's lifespan starts (get_pool reads DATABASE_URL at startup).
    Without this dependency, e2e_app may start the lifespan before test_pool
    has written the URL, causing get_pool to connect to the wrong database.
    """
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
async def test_nps_score_saved_after_delivery(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    NPS score is saved to nps_responses after delivery is marked entregado.

    Turn 1: "Hola, quiero pedir a domicilio empanaditas"
    Turn 2: address (or GPS if bot requests)
    Turn 3: "Efectivo"
    Turn 4: "sí confirmo"
    Admin: PATCH order → entregado  (trigger_nps awaited synchronously here)
    Turn 5: "5"  (NPS score)

    Assert: nps_responses row with score=5 for this phone+org.
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E NPS Test Restaurant",
        bot_number_raw="+570E2ENPSTST",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),
            (4.609710, -74.081741),
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    # Clean volatile data from prior runs
    await truncate_e2e_data(pool, parent_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    # Reset in-process fallback state (NPS, checkout, cart locks)
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits",
                     "_fb_nps_transition_locks", "_fb_nps_transition_owner"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info(
        "e2e.nps.test_start",
        parent_id=parent_id,
        bot_number=bot_number,
        customer_phone=CUSTOMER_PHONE,
    )

    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # ── Turns 1-4: place delivery order ───────────────────────────────────────

    turn1_text = "Hola, quiero pedir a domicilio empanaditas"
    log.info("e2e.nps.turn_1", text=turn1_text)
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=turn1_text, bot_number=bot_number,
    )
    log.info("e2e.nps.turn_1_done", processed=processed_1, elapsed_s=round(time.monotonic() - t1_start, 1))
    assert processed_1 >= 1, "Turn 1 not processed"
    assert len(wa_capture.texts_to(CUSTOMER_PHONE_RAW)) >= 1, "Turn 1: bot sent no reply"

    # Turn 2: text address
    turn2_text = f"Mi dirección es {CUSTOMER_ADDRESS}"
    log.info("e2e.nps.turn_2", text=turn2_text)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=turn2_text, bot_number=bot_number,
    )
    log.info("e2e.nps.turn_2_done", processed=processed_2, elapsed_s=round(time.monotonic() - t2_start, 1))
    assert processed_2 >= 1, "Turn 2 not processed"

    # Turn 2b: GPS if bot still asks for location
    t2b_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    needs_gps = any(
        any(kw in r.lower() for kw in ["ubicación", "ubicacion", "gps", "comparte", "localiz"])
        for r in t2b_replies[-2:]
    )
    if needs_gps:
        log.info("e2e.nps.turn_2b_gps")
        processed_2b = await simulate_whatsapp_inbound(
            client, pool, phone=CUSTOMER_PHONE_RAW, text="",
            bot_number=bot_number, lat=CUSTOMER_LAT, lon=CUSTOMER_LON,
        )
        log.info("e2e.nps.turn_2b_done", processed=processed_2b)
        assert processed_2b >= 1, "Turn 2b GPS not processed"

    # Turn 3: payment method
    turn3_text = "Efectivo"
    log.info("e2e.nps.turn_3", text=turn3_text)
    t3_start = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=turn3_text, bot_number=bot_number,
    )
    log.info("e2e.nps.turn_3_done", processed=processed_3, elapsed_s=round(time.monotonic() - t3_start, 1))
    assert processed_3 >= 1, "Turn 3 not processed"

    # Turn 4: confirm — unambiguous phrasing to stop LLM from asking for
    # clarification. Plain "sí confirmo" intermittently made the model re-prompt
    # instead of calling create_delivery_order, breaking downstream NPS flow.
    turn4_text = (
        "Sí, confirmo mi pedido completo: empanadita de carne, "
        "pago en efectivo, dirección ya indicada. Procede con el pedido."
    )
    log.info("e2e.nps.turn_4", text=turn4_text)
    t4_start = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=turn4_text, bot_number=bot_number,
    )
    log.info("e2e.nps.turn_4_done", processed=processed_4, elapsed_s=round(time.monotonic() - t4_start, 1))
    assert processed_4 >= 1, "Turn 4 not processed"

    # ── Find the created order ─────────────────────────────────────────────────
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_nps_assert_order_created"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """SELECT id, phone, status, org_id, bot_number
                       FROM orders
                       WHERE org_id=$1 AND phone=$2 AND order_type='domicilio'
                       ORDER BY created_at DESC LIMIT 1""",
                    parent_id, CUSTOMER_PHONE,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No delivery order found for phone={CUSTOMER_PHONE}, org_id={parent_id}. "
        "Check turns 1-4 succeeded and the order was committed."
    )
    order_id = order_row["id"]
    log.info("e2e.nps.order_found", order_id=order_id, status=order_row["status"])

    # ── Admin: PATCH → confirmado → en_preparacion → en_camino → entregado ─────
    for transition_status in ["confirmado", "en_preparacion", "en_camino"]:
        log.info("e2e.nps.patch_status", status=transition_status)
        patch_resp = await client.patch(
            f"/api/delivery/orders/{order_id}/status",
            json={"status": transition_status},
            headers=auth_headers,
        )
        assert patch_resp.status_code == 200, (
            f"PATCH to {transition_status} failed: {patch_resp.status_code} {patch_resp.text}"
        )
        # Allow background WA task a moment to fire (non-blocking for NPS)
        await asyncio.sleep(0.3)

    # PATCH to entregado — trigger_nps is now awaited INSIDE update_delivery_status
    # before the response returns. So by the time this returns, nps_set is committed.
    log.info("e2e.nps.patch_entregado", order_id=order_id)
    entregado_resp = await client.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "entregado"},
        headers=auth_headers,
    )
    assert entregado_resp.status_code == 200, (
        f"PATCH to entregado failed: {entregado_resp.status_code} {entregado_resp.text}"
    )
    log.info("e2e.nps.patch_entregado_done")

    # At this point trigger_nps has already completed (awaited in the handler).
    # Verify in-process state_store has the NPS entry.
    import app.services.state_store as ss
    nps_state = await ss.nps_get(CUSTOMER_PHONE, bot_number)
    log.info("e2e.nps.state_after_entregado", nps_state=nps_state)
    assert nps_state is not None, (
        "NPS state was not set in state_store after PATCH entregado. "
        "trigger_nps may have raised or skipped (check nps_is_done guard / lock contention)."
    )
    assert nps_state.get("state") == "waiting_score", (
        f"NPS state has unexpected value: {nps_state}"
    )

    # ── Turn 5: customer sends "5" ─────────────────────────────────────────────
    turn5_text = "5"
    log.info("e2e.nps.turn_5_score", text=turn5_text)
    t5_start = time.monotonic()
    processed_5 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=turn5_text, bot_number=bot_number,
    )
    log.info("e2e.nps.turn_5_done", processed=processed_5, elapsed_s=round(time.monotonic() - t5_start, 1))
    assert processed_5 >= 1, "Turn 5 (NPS score) not processed"

    # Bot should have replied with a thank-you for a high score
    turn5_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.nps.turn_5_replies", count=len(turn5_replies), last_replies=turn5_replies[-3:])
    assert len(turn5_replies) >= 1, "Turn 5: bot sent no reply after NPS score"

    # ── Assert: nps_responses row exists with score=5 ─────────────────────────
    nps_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_nps_assert_score_saved"):
            async with pool.acquire() as conn:
                nps_row = await conn.fetchrow(
                    """SELECT id, score, comment, org_id
                       FROM nps_responses
                       WHERE org_id=$1
                         AND phone=$2
                         AND score = 5
                       ORDER BY created_at DESC LIMIT 1""",
                    parent_id, CUSTOMER_PHONE,
                )
        if nps_row:
            break
        await asyncio.sleep(0.5)

    assert nps_row is not None, (
        f"nps_responses row with score=5 not found for phone={CUSTOMER_PHONE}, org_id={parent_id}. "
        "Root cause: _try_nps_active_flow returned None (nps_get returned None), "
        "meaning trigger_nps had not yet committed nps_set when the score arrived. "
        "Fix: await trigger_nps in update_delivery_status before launching WA background task."
    )
    assert nps_row["score"] == 5, (
        f"Expected score=5, got score={nps_row['score']}"
    )
    assert nps_row["org_id"] == parent_id, (
        f"nps_responses.org_id={nps_row['org_id']} does not match parent_id={parent_id}"
    )
    log.info(
        "e2e.nps.score_saved",
        nps_id=nps_row["id"],
        score=nps_row["score"],
        comment=nps_row["comment"],
    )
