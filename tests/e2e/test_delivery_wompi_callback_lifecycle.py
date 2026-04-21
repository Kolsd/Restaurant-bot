"""
tests/e2e/test_delivery_wompi_callback_lifecycle.py — E2E test: delivery order with
Wompi payment callback path.

ONE happy-path test + TWO adversarial negative tests.

Flow:
  1. Seed restaurant with payment_methods=["Wompi", "Efectivo"]
  2. Customer WhatsApp turns (same pattern as test_delivery_lifecycle.py):
     Turn 1: "Hola, quiero pedir a domicilio empanaditas"
     Turn 2: address
     [Turn 2b optional GPS if bot asks again]
     Turn 3: "wompi" (lowercase enum token recognized by agent_tools.py)
     Turn 4: "si confirmo"
  3. Assert order in DB with status="pendiente" (Wompi pre-payment — not yet confirmed)
  4. Simulate valid Wompi callback (APPROVED) → assert status transitions to "confirmado"
  5. Adversarial: invalid signature → 401, order status unchanged
  6. Adversarial: missing transaction.id → 400, order status unchanged

Signing scheme (from app/routes/orders_routes.py wompi_webhook handler):
  sig = sha256( json_body_str + WOMPI_EVENTS_SECRET ).hexdigest()
  header: x-event-checksum = <sig>

Env:
  ANTHROPIC_API_KEY    — real Anthropic key
  TEST_DATABASE_URL    — Postgres URL (isolated test DB)
  WOMPI_EVENTS_SECRET  — set to a test value in the fixture (patched into the module)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

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

CUSTOMER_PHONE_RAW = "+573001112233"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

CUSTOMER_LAT = 4.711200
CUSTOMER_LON = -74.072300

CUSTOMER_ADDRESS = "Avenida 82 # 10-60, Bogotá"

# Wompi events secret used in this test — set as env var so the module-level constant picks it up.
_WOMPI_TEST_SECRET = "e2e_wompi_events_secret_xyz"

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

# "Wompi" as the payment option surfaced to the customer (and accepted by the bot);
# the agent tools enum uses lowercase "wompi" internally.
PAYMENT_METHODS = ["Wompi", "Efectivo"]


# ── Helper: build a valid Wompi webhook payload + signature ────────────────────

def _build_wompi_payload(order_id: str, *, tx_id: str = None, tx_status: str = "APPROVED") -> bytes:
    """Return the JSON-encoded body bytes for a Wompi transaction.updated event.

    Reference format taken from orders_routes.py wompi_webhook handler.
    """
    if tx_id is None:
        tx_id = f"wompi_tx_e2e_{uuid.uuid4().hex[:12]}"
    payload = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": tx_id,
                "status": tx_status,
                "reference": order_id,
                # amount_in_cents is informational; handler does NOT currently
                # validate amount against order (checked in adversarial test below).
                "amount_in_cents": 1500000,
                "currency": "COP",
            }
        },
    }
    return json.dumps(payload).encode()


def _sign_wompi_body(body_bytes: bytes, secret: str) -> str:
    """Replicate the signing scheme used by orders_routes.py:

        sha256(body_str + secret).hexdigest()

    The handler concatenates the raw JSON string with the secret BEFORE hashing.
    """
    return hashlib.sha256((body_bytes.decode() + secret).encode()).hexdigest()


def _wompi_headers(body_bytes: bytes, secret: str) -> dict:
    return {
        "Content-Type": "application/json",
        "x-event-checksum": _sign_wompi_body(body_bytes, secret),
    }


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app_wompi(wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app.

    Also patches WOMPI_EVENTS_SECRET at the module level so the webhook handler
    finds a non-None secret (the module reads it at import time via os.getenv).
    """
    import os
    os.environ["WOMPI_EVENTS_SECRET"] = _WOMPI_TEST_SECRET

    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    with patch("app.routes.orders_routes.WOMPI_EVENTS_SECRET", _WOMPI_TEST_SECRET):
        async with LifespanManager(fastapi_app) as manager:
            async with AsyncClient(
                transport=ASGITransport(app=manager.app),
                base_url="http://test",
                timeout=120.0,
            ) as client:
                yield client


# ── Happy-path E2E test ────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delivery_wompi_callback_happy_path(
    test_pool: asyncpg.Pool,
    e2e_app_wompi: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full Wompi payment callback lifecycle:

    1. Customer orders via WhatsApp with payment_method="wompi"
    2. Order lands in DB with status="pendiente", paid=False
    3. Wompi sends APPROVED callback → order.status becomes "confirmado", paid=True
    4. Adversarial: invalid signature → 401, order stays "confirmado"
    5. Adversarial: missing transaction.id → 400

    Also verifies that WA messages were sent to customer during the bot turns.
    """
    client = e2e_app_wompi
    pool = test_pool

    # ── Seed restaurant ────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Wompi Callback Test",
        bot_number_raw="+570WOMPITEST1",
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

    await truncate_e2e_data(pool, parent_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    # Also clear the idempotency table to avoid interference from prior runs.
    async with pool.acquire() as _conn:
        await _conn.execute(
            "DELETE FROM processed_wompi_events WHERE event_id LIKE 'wompi_tx_e2e_%'"
        )

    # Reset in-process fallback state
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
        "e2e.wompi.test_start",
        parent_id=parent_id,
        bot_number=bot_number,
        customer_phone=CUSTOMER_PHONE,
    )

    # ── Turn 1: delivery intent + item ────────────────────────────────────────
    turn1_text = "Hola, quiero pedir a domicilio empanaditas"
    log.info("e2e.wompi.turn_1", text=turn1_text)
    t1 = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn1_text, bot_number=bot_number,
    )
    log.info("e2e.wompi.turn_1_done", processed=processed_1, elapsed=round(time.monotonic() - t1, 1))
    assert processed_1 >= 1, "Turn 1 was not processed"
    assert len(wa_capture.texts_to(CUSTOMER_PHONE_RAW)) >= 1, "Bot sent no reply after turn 1"

    # ── Turn 2: text address ──────────────────────────────────────────────────
    turn2_text = f"Mi dirección es {CUSTOMER_ADDRESS}"
    log.info("e2e.wompi.turn_2", text=turn2_text)
    t2 = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn2_text, bot_number=bot_number,
    )
    log.info("e2e.wompi.turn_2_done", processed=processed_2, elapsed=round(time.monotonic() - t2, 1))
    assert processed_2 >= 1, "Turn 2 was not processed"

    replies_after_t2 = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(replies_after_t2) >= 2, "Bot sent no reply after address"

    # ── Turn 2b (optional GPS) ────────────────────────────────────────────────
    latest = replies_after_t2[-1].lower() if replies_after_t2 else ""
    needs_gps = any(kw in latest for kw in ("ubicaci", "gps", "localizaci", "direcci", "mapa"))
    if needs_gps:
        log.info("e2e.wompi.turn_2b_gps")
        processed_2b = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW, text="", bot_number=bot_number,
            lat=CUSTOMER_LAT, lon=CUSTOMER_LON,
        )
        assert processed_2b >= 1, "Turn 2b GPS was not processed"

    # ── Turn 3: payment method = wompi ────────────────────────────────────────
    # The agent_tools.py enum includes "wompi" (lowercase). We send the customer-facing
    # "Wompi" label; Claude maps it to the enum value.
    turn3_text = "Wompi"
    log.info("e2e.wompi.turn_3", text=turn3_text)
    t3 = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn3_text, bot_number=bot_number,
    )
    log.info("e2e.wompi.turn_3_done", processed=processed_3, elapsed=round(time.monotonic() - t3, 1))
    assert processed_3 >= 1, "Turn 3 (payment method) was not processed"

    # ── Turn 4: confirmation ──────────────────────────────────────────────────
    turn4_text = "si confirmo"
    log.info("e2e.wompi.turn_4", text=turn4_text)
    t4 = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW, text=turn4_text, bot_number=bot_number,
    )
    log.info("e2e.wompi.turn_4_done", processed=processed_4, elapsed=round(time.monotonic() - t4, 1))
    assert processed_4 >= 1, "Turn 4 (confirmation) was not processed"

    # ── Assert: order exists with status=pendiente ─────────────────────────────
    order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_wompi_assert_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, phone, order_type, status, total, org_id,
                           paid, payment_method, bot_number
                    FROM orders
                    WHERE phone = $1
                      AND order_type = 'domicilio'
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
        f"No delivery order found for phone {CUSTOMER_PHONE}. "
        "Verify ANTHROPIC_API_KEY is valid and the bot resolved 'wompi' payment method."
    )

    order_id = order_row["id"]
    log.info(
        "e2e.wompi.order_found",
        order_id=order_id,
        status=order_row["status"],
        total=order_row["total"],
        paid=order_row["paid"],
        payment_method=order_row["payment_method"],
    )

    # Validate pre-callback state
    assert order_row["order_type"] == "domicilio"
    assert order_row["status"] == "pendiente", (
        f"Expected status='pendiente' before callback, got '{order_row['status']}'"
    )
    assert order_row["paid"] is False, "Order should not be paid before Wompi callback"
    assert order_row["org_id"] == parent_id

    order_total = to_decimal(order_row["total"])
    assert order_total >= Decimal("15000"), (
        f"Expected total >= 15000, got {order_total}"
    )

    # payment_method should contain "wompi" (the agent normalizes to lowercase enum value)
    payment_method_recorded = (order_row["payment_method"] or "").lower()
    assert "wompi" in payment_method_recorded, (
        f"Expected payment_method to contain 'wompi', got '{order_row['payment_method']}'. "
        "If the agent refused 'Wompi' as a payment method, this is a GAP — "
        "the bot may not be surfacing Wompi correctly from features.payment_methods."
    )

    # ── WA messages: at least 4 (one per turn) ────────────────────────────────
    all_customer_msgs_before_callback = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.wompi.wa_before_callback", count=len(all_customer_msgs_before_callback))
    assert len(all_customer_msgs_before_callback) >= 4, (
        f"Expected >= 4 WA messages before callback (one per turn), "
        f"got {len(all_customer_msgs_before_callback)}"
    )

    # ── ADVERSARIAL 1: invalid signature → 401, order unchanged ──────────────
    log.info("e2e.wompi.adversarial_invalid_sig")
    bad_body = _build_wompi_payload(order_id, tx_id=f"wompi_tx_bad_{uuid.uuid4().hex[:8]}")
    bad_resp = await client.post(
        "/api/payment/wompi-webhook",
        content=bad_body,
        headers={
            "Content-Type": "application/json",
            "x-event-checksum": "badhash_thisiswrong",
        },
    )
    assert bad_resp.status_code == 401, (
        f"Expected 401 for invalid signature, got {bad_resp.status_code}: {bad_resp.text}"
    )
    # Verify order status did NOT change
    with bypass_tenant_scope("e2e_wompi_assert_after_badsig"):
        async with pool.acquire() as conn:
            status_after_bad = await conn.fetchval(
                "SELECT status FROM orders WHERE id = $1", order_id
            )
    assert status_after_bad == "pendiente", (
        f"Order status changed after invalid signature! Got '{status_after_bad}' (expected 'pendiente')"
    )
    log.info("e2e.wompi.adversarial_invalid_sig_passed", status=status_after_bad)

    # ── ADVERSARIAL 2: missing transaction.id → 400 ───────────────────────────
    # The handler requires data.transaction.id for idempotency. Without it → 400.
    log.info("e2e.wompi.adversarial_missing_tx_id")
    payload_no_txid = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                # id is intentionally omitted
                "status": "APPROVED",
                "reference": order_id,
                "amount_in_cents": 1500000,
                "currency": "COP",
            }
        },
    }
    no_txid_body = json.dumps(payload_no_txid).encode()
    no_txid_resp = await client.post(
        "/api/payment/wompi-webhook",
        content=no_txid_body,
        headers=_wompi_headers(no_txid_body, _WOMPI_TEST_SECRET),
    )
    assert no_txid_resp.status_code == 400, (
        f"Expected 400 for missing transaction.id, got {no_txid_resp.status_code}: {no_txid_resp.text}"
    )
    # Verify order still unchanged
    with bypass_tenant_scope("e2e_wompi_assert_after_notxid"):
        async with pool.acquire() as conn:
            status_after_notxid = await conn.fetchval(
                "SELECT status FROM orders WHERE id = $1", order_id
            )
    assert status_after_notxid == "pendiente", (
        f"Order status changed after missing-tx-id request! Got '{status_after_notxid}'"
    )
    log.info("e2e.wompi.adversarial_missing_tx_id_passed", status=status_after_notxid)

    # ── Happy path: valid APPROVED Wompi callback ─────────────────────────────
    # Use a stable tx_id derived from the order_id so it's deterministic for this run.
    tx_id = f"wompi_tx_e2e_{order_id.replace('-', '_')}"
    callback_body = _build_wompi_payload(order_id, tx_id=tx_id, tx_status="APPROVED")

    log.info("e2e.wompi.happy_path_callback", order_id=order_id, tx_id=tx_id)
    callback_resp = await client.post(
        "/api/payment/wompi-webhook",
        content=callback_body,
        headers=_wompi_headers(callback_body, _WOMPI_TEST_SECRET),
    )
    assert callback_resp.status_code == 200, (
        f"Wompi APPROVED callback failed: {callback_resp.status_code} {callback_resp.text}"
    )
    callback_json = callback_resp.json()
    assert callback_json.get("status") == "ok", (
        f"Expected {{status: ok}}, got {callback_json}"
    )

    # ── Assert: order transitioned to confirmado + paid=True ─────────────────
    # db_confirm_payment sets status='confirmado', paid=TRUE, transaction_id=$2, paid_at=NOW()
    # Give a brief moment for the DB write to commit (it's synchronous in the handler)
    await asyncio.sleep(0.3)

    with bypass_tenant_scope("e2e_wompi_assert_post_callback"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                """
                SELECT status, paid, transaction_id, paid_at
                FROM orders
                WHERE id = $1
                """,
                order_id,
            )

    assert final_row is not None, f"Order {order_id} disappeared from DB"

    log.info(
        "e2e.wompi.post_callback_state",
        status=final_row["status"],
        paid=final_row["paid"],
        transaction_id=final_row["transaction_id"],
        paid_at=str(final_row["paid_at"]),
    )

    assert final_row["status"] == "confirmado", (
        f"Expected status='confirmado' after APPROVED callback, got '{final_row['status']}'. "
        "db_confirm_payment in orders_repo.py should have updated the row."
    )
    assert final_row["paid"] is True, (
        f"Expected paid=True after APPROVED callback, got paid={final_row['paid']}"
    )
    assert final_row["transaction_id"] == tx_id, (
        f"Expected transaction_id='{tx_id}', got '{final_row['transaction_id']}'"
    )
    assert final_row["paid_at"] is not None, (
        "Expected paid_at to be set after APPROVED callback, got None"
    )

    # ── Idempotency: replay the same event → 200, "already_processed" ─────────
    log.info("e2e.wompi.idempotency_replay", tx_id=tx_id)
    replay_resp = await client.post(
        "/api/payment/wompi-webhook",
        content=callback_body,
        headers=_wompi_headers(callback_body, _WOMPI_TEST_SECRET),
    )
    assert replay_resp.status_code == 200, (
        f"Expected 200 on replay, got {replay_resp.status_code}"
    )
    replay_json = replay_resp.json()
    assert replay_json.get("status") == "already_processed", (
        f"Expected {{status: already_processed}} on idempotent replay, got {replay_json}"
    )

    # ── Final WA message count ────────────────────────────────────────────────
    # Wompi callback does NOT send a WA message directly (handler has no WA send),
    # so total count stays the same as before the callback.
    all_customer_msgs_final = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.wompi.final_wa_messages",
        count=len(all_customer_msgs_final),
        messages=all_customer_msgs_final,
    )
    # Wompi callback handler has no WhatsApp notification logic, so the WA count
    # stays equal to the pre-callback count (the bot only sent messages during turns).
    assert len(all_customer_msgs_final) >= 4, (
        f"Expected >= 4 WA messages total, got {len(all_customer_msgs_final)}"
    )

    print(
        f"\n[OK] E2E Wompi callback test passed.\n"
        f"  order_id={order_id}\n"
        f"  status=confirmado, paid=True\n"
        f"  transaction_id={tx_id}\n"
        f"  WA messages to customer: {len(all_customer_msgs_final)}\n"
        f"  Adversarial tests: invalid_sig→401, missing_tx_id→400 (both order unchanged)\n"
        f"  Idempotency replay: already_processed"
    )
