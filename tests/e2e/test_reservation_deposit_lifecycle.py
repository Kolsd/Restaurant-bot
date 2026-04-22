"""
tests/e2e/test_reservation_deposit_lifecycle.py — E2E test: reservation deposit
(Wompi prepayment) lifecycle.

Flow:
  1. Seed restaurant with reservation_deposits=True + reservation_deposit_amount=50000
  2. Create 1 restaurant_table via POST /api/tables (required for availability)
  3. Turn 1: customer requests reservation for 4 ppl next week at 19:00
  4. Turn 2 (conditional): confirm if bot prompts
  5. Assert reservations row exists
  6. Assert reservation_deposits row exists with amount=50000, status='pending',
     non-empty payment_url
  7. Simulate Wompi APPROVED callback for reference "dep_{reservation_id}"
  8. Assert response 200
  9. Assert reservation_deposits.status == 'paid'
  10. Assert reservations.status == 'confirmed' AND deposit_paid == True

Reference format (from reservation_payments.py line 51):
    reference = f"dep_{reservation_id}"

Wompi signing (same scheme as orders_routes.py wompi_webhook):
    sig = sha256(body_str + WOMPI_EVENTS_SECRET).hexdigest()
    header: x-event-checksum = <sig>

Env:
    ANTHROPIC_API_KEY     — real Anthropic key
    TEST_DATABASE_URL     — Postgres URL (isolated test DB)
    WOMPI_EVENTS_SECRET   — set via env (patch applied to module)
    WOMPI_INTEGRITY_SECRET — must be set so generate_deposit_link doesn't raise RuntimeError
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import date, timedelta

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
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573007771234"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

_RESERVATION_DATE: date = date.today() + timedelta(days=7)
RESERVATION_DATE_STR: str = _RESERVATION_DATE.strftime("%Y-%m-%d")
RESERVATION_GUESTS: int = 4
RESERVATION_NAME: str = "Laura Morales"

MENU = {
    "Entradas": [
        {
            "name": "Empanaditas de Carne (3 uds)",
            "description": "Empanadas fritas rellenas de carne y papa",
            "price": 15000,
            "active": True,
        }
    ]
}

_WOMPI_TEST_SECRET = "e2e_test_events_secret_xyz"
# integrity_secret required by generate_deposit_link (fails-fast if missing)
_WOMPI_INTEGRITY_SECRET = "e2e_integrity_secret_test"
_DEPOSIT_AMOUNT = 50000  # matches features reservation_deposit_amount


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_deposit_wompi_payload(reservation_id: int, *, tx_id: str | None = None) -> bytes:
    """Build a Wompi APPROVED transaction.updated payload for a deposit reference."""
    if tx_id is None:
        tx_id = f"wompi_dep_e2e_{uuid.uuid4().hex[:12]}"
    reference = f"dep_{reservation_id}"
    payload = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": tx_id,
                "status": "APPROVED",
                "reference": reference,
                "amount_in_cents": _DEPOSIT_AMOUNT,  # COP no-cents multiplier
                "currency": "COP",
            }
        },
    }
    return json.dumps(payload).encode()


def _sign_wompi(body_bytes: bytes, secret: str) -> str:
    """Replicate the signing scheme from orders_routes.py wompi_webhook."""
    return hashlib.sha256((body_bytes.decode() + secret).encode()).hexdigest()


def _wompi_headers(body_bytes: bytes, secret: str) -> dict:
    return {
        "Content-Type": "application/json",
        "x-event-checksum": _sign_wompi(body_bytes, secret),
    }


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app_deposit(wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app.

    Patches:
    - WOMPI_EVENTS_SECRET in orders_routes so the webhook handler finds the secret.
    - WOMPI_INTEGRITY_SECRET in reservation_payments so generate_deposit_link
      doesn't raise RuntimeError (it checks this at call time, not import time).
    """
    import os
    os.environ["WOMPI_EVENTS_SECRET"] = _WOMPI_TEST_SECRET
    os.environ["WOMPI_INTEGRITY_SECRET"] = _WOMPI_INTEGRITY_SECRET
    os.environ["WOMPI_PUBLIC_KEY"] = "pub_test_e2e"

    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    with (
        patch("app.routes.orders_routes.WOMPI_EVENTS_SECRET", _WOMPI_TEST_SECRET),
        patch("app.services.reservation_payments.WOMPI_INTEGRITY_SECRET", _WOMPI_INTEGRITY_SECRET),
        patch("app.services.reservation_payments.WOMPI_PUBLIC_KEY", "pub_test_e2e"),
    ):
        async with LifespanManager(fastapi_app) as manager:
            async with AsyncClient(
                transport=ASGITransport(app=manager.app),
                base_url="http://test",
                timeout=120.0,
            ) as client:
                yield client


# ── Test ───────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_reservation_deposit_wompi_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app_deposit: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full reservation deposit (Wompi prepayment) lifecycle:

    1. Customer requests reservation (deposits enabled) via WhatsApp
    2. Bot creates reservation + deposit row + sends Wompi link
    3. Wompi APPROVED callback → deposit.status='paid', reservation.status='confirmed'
    """
    client = e2e_app_deposit
    pool = test_pool

    # ── Seed ──────────────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Deposit Test Restaurant",
        bot_number_raw="+570E2EDEPOSIT",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=0,  # single sede — availability resolves from principal bot_number
        features_override={
            "module_reservations": True,
            "reservation_deposits": True,
            "reservation_deposit_amount": _DEPOSIT_AMOUNT,
        },
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, parent_id)
    # Clean reservations + deposits for this org
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM reservation_deposits WHERE reservation_id IN "
                           "(SELECT id FROM reservations WHERE org_id = $1)", parent_id)
        await conn.execute("DELETE FROM reservations WHERE org_id = $1", parent_id)
    # Clear idempotency table from prior runs
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM processed_wompi_events WHERE event_id LIKE 'wompi_dep_e2e_%'"
        )

    # Reset in-process state store fallback
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info("e2e.deposit_test_start", parent_id=parent_id, bot_number=bot_number)

    # ── Admin token + create table ────────────────────────────────────────────
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    create_table_resp = await client.post("/api/tables", headers=auth_headers)
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_id: str = create_table_resp.json()["table_id"]
    log.info("e2e.deposit_table_created", table_id=table_id)

    # ── Turn 1: reservation request ───────────────────────────────────────────
    reservation_text = (
        f"Hola, quiero hacer una reserva para {RESERVATION_GUESTS} personas "
        f"el {RESERVATION_DATE_STR} a las 7 pm, a nombre de {RESERVATION_NAME}"
    )
    log.info("e2e.deposit_turn_1", text=reservation_text)
    t1 = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=reservation_text,
        bot_number=bot_number,
    )
    log.info("e2e.deposit_turn_1_done", processed=processed_1, elapsed=round(time.monotonic() - t1, 1))
    assert processed_1 >= 1, "Turn 1 was not processed"
    assert len(wa_capture.texts_to(CUSTOMER_PHONE_RAW)) >= 1, "Bot sent no reply after turn 1"

    # ── Turn 2 (conditional): confirm ─────────────────────────────────────────
    confirm_text = "Sí, confirmo la reserva"
    log.info("e2e.deposit_turn_2", text=confirm_text)
    t2 = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=confirm_text,
        bot_number=bot_number,
    )
    log.info("e2e.deposit_turn_2_done", processed=processed_2, elapsed=round(time.monotonic() - t2, 1))
    assert processed_2 >= 1, "Turn 2 was not processed"

    # ── Assert: reservations row ──────────────────────────────────────────────
    reservation_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_deposit_assert_reservation"):
            async with pool.acquire() as conn:
                reservation_row = await conn.fetchrow(
                    """
                    SELECT id, phone, guests, status, deposit_paid, org_id
                    FROM reservations
                    WHERE org_id = $1
                      AND phone = $2
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    parent_id,
                    CUSTOMER_PHONE,
                )
        if reservation_row:
            break
        await asyncio.sleep(0.5)

    assert reservation_row is not None, (
        f"No reservation found for phone={CUSTOMER_PHONE} org_id={parent_id}. "
        "Check that the bot called make_reservation and the table was created correctly."
    )
    reservation_id = reservation_row["id"]
    log.info(
        "e2e.deposit_reservation_found",
        reservation_id=reservation_id,
        status=reservation_row["status"],
        deposit_paid=reservation_row["deposit_paid"],
    )

    # ── Assert: reservation_deposits row ─────────────────────────────────────
    deposit_row = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_deposit_assert_deposit_row"):
            async with pool.acquire() as conn:
                deposit_row = await conn.fetchrow(
                    """
                    SELECT id, reservation_id, amount, status, payment_url, currency
                    FROM reservation_deposits
                    WHERE reservation_id = $1
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    reservation_id,
                )
        if deposit_row:
            break
        await asyncio.sleep(0.3)

    assert deposit_row is not None, (
        f"No reservation_deposit row found for reservation_id={reservation_id}. "
        "Check that generate_deposit_link was called by the agent after make_reservation "
        "and that WOMPI_INTEGRITY_SECRET was patched correctly."
    )
    log.info(
        "e2e.deposit_deposit_row_found",
        deposit_id=deposit_row["id"],
        amount=str(deposit_row["amount"]),
        status=deposit_row["status"],
        payment_url_prefix=deposit_row["payment_url"][:60] if deposit_row["payment_url"] else "",
    )

    # Exact observed status before callback
    assert deposit_row["status"] == "pending", (
        f"Expected deposit status='pending' before callback, got '{deposit_row['status']}'"
    )
    assert deposit_row["amount"] == _DEPOSIT_AMOUNT or float(deposit_row["amount"]) == float(_DEPOSIT_AMOUNT), (
        f"Expected amount={_DEPOSIT_AMOUNT}, got {deposit_row['amount']}"
    )
    assert deposit_row["payment_url"] and len(deposit_row["payment_url"]) > 10, (
        f"Expected non-empty payment_url, got '{deposit_row['payment_url']}'"
    )
    assert deposit_row["reservation_id"] == reservation_id, (
        f"Deposit points to wrong reservation: {deposit_row['reservation_id']} != {reservation_id}"
    )

    # ── Build + POST Wompi APPROVED callback ─────────────────────────────────
    tx_id = f"wompi_dep_e2e_{uuid.uuid4().hex[:12]}"
    callback_body = _build_deposit_wompi_payload(reservation_id, tx_id=tx_id)

    log.info(
        "e2e.deposit_wompi_callback",
        reservation_id=reservation_id,
        reference=f"dep_{reservation_id}",
        tx_id=tx_id,
    )
    callback_resp = await client.post(
        "/api/payment/wompi-webhook",
        content=callback_body,
        headers=_wompi_headers(callback_body, _WOMPI_TEST_SECRET),
    )
    assert callback_resp.status_code == 200, (
        f"Wompi deposit callback failed: {callback_resp.status_code} {callback_resp.text}"
    )
    callback_json = callback_resp.json()
    assert callback_json.get("status") == "ok", (
        f"Expected {{status: ok}}, got {callback_json}"
    )

    # ── Assert: deposit transitioned to 'paid' ────────────────────────────────
    await asyncio.sleep(0.3)

    with bypass_tenant_scope("e2e_deposit_assert_paid"):
        async with pool.acquire() as conn:
            final_deposit = await conn.fetchrow(
                "SELECT status, paid_at, transaction_id FROM reservation_deposits "
                "WHERE reservation_id = $1 ORDER BY created_at ASC LIMIT 1",
                reservation_id,
            )

    assert final_deposit is not None, "Deposit row disappeared after callback"
    log.info(
        "e2e.deposit_post_callback",
        deposit_status=final_deposit["status"],
        paid_at=str(final_deposit["paid_at"]),
        transaction_id=final_deposit["transaction_id"],
    )

    # Exact terminal status from db_confirm_deposit_and_reservation (line 80 of repo):
    # SET status = 'paid'
    assert final_deposit["status"] == "paid", (
        f"Expected deposit status='paid' after APPROVED callback, got '{final_deposit['status']}'"
    )
    assert final_deposit["paid_at"] is not None, "Expected paid_at to be set, got None"
    assert final_deposit["transaction_id"] == tx_id, (
        f"Expected transaction_id='{tx_id}', got '{final_deposit['transaction_id']}'"
    )

    # ── Assert: reservation transitioned to 'confirmed' + deposit_paid=True ──
    with bypass_tenant_scope("e2e_deposit_assert_reservation_confirmed"):
        async with pool.acquire() as conn:
            final_reservation = await conn.fetchrow(
                "SELECT status, deposit_paid FROM reservations WHERE id = $1",
                reservation_id,
            )

    assert final_reservation is not None, "Reservation row disappeared after callback"
    log.info(
        "e2e.deposit_reservation_post_callback",
        reservation_status=final_reservation["status"],
        deposit_paid=final_reservation["deposit_paid"],
    )

    # db_confirm_deposit_and_reservation sets status='confirmed' (line 141 of repo)
    assert final_reservation["status"] == "confirmed", (
        f"Expected reservation status='confirmed' after deposit payment, "
        f"got '{final_reservation['status']}'"
    )
    # Note: deposit_paid column is updated by db_confirm_deposit_and_reservation
    # which transitions the reservation to 'confirmed'. The deposit_paid flag
    # reflects payment was received.
    assert final_reservation["deposit_paid"] is True or final_reservation["deposit_paid"] == True, (
        f"Expected deposit_paid=True after APPROVED callback, got {final_reservation['deposit_paid']}"
    )

    print(
        f"\n[OK] Reservation deposit lifecycle test passed.\n"
        f"  reservation_id={reservation_id}\n"
        f"  deposit: pending → paid (tx_id={tx_id})\n"
        f"  reservation: → confirmed, deposit_paid=True\n"
        f"  WA messages: {len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))}"
    )
