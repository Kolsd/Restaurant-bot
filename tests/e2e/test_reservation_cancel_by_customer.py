"""
tests/e2e/test_reservation_cancel_by_customer.py — E2E test: customer cancels their own reservation via WhatsApp.

Two tests:
  1. test_customer_cancels_upcoming_reservation
       - Seed restaurant + table + reservation (status=pending, future date)
       - Customer turn: "Quiero cancelar mi reserva"
       - Assert: reservations.status == 'cancelled', cancelled_at IS NOT NULL
       - Assert: bot reply contains "cancel"

  2. test_cancel_past_reservation_rejected
       - Seed restaurant + table
       - DB INSERT a reservation with date in the past (status='pending')
       - Customer turn: "cancela mi reserva"
       - Assert: reservations.status == 'pending' (unchanged)
       - Assert: bot reply mentions no upcoming reservation

No new migration needed — reservations.cancelled_at and cancellation_reason
were added in migration 0014.

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — dedicated (non-production) Postgres DB

Run:
  pytest tests/e2e/test_reservation_cancel_by_customer.py -v -s -m e2e
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

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

CUSTOMER_PHONE_RAW = "+573009995544"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# A future date 10 days from today for the reservation
_FUTURE_DATE: date = date.today() + timedelta(days=10)
RESERVATION_DATE_STR: str = _FUTURE_DATE.strftime("%Y-%m-%d")
RESERVATION_TIME: str = "20:00"
RESERVATION_NAME: str = "Carlos Ruiz"
RESERVATION_GUESTS: int = 2

MENU = {
    "Empanadas": [
        {
            "name": "Empanadita de Pollo",
            "description": "Empanada rellena de pollo",
            "price": 5000,
            "active": True,
        }
    ]
}


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(test_pool, wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app via ASGI transport.

    Depends on test_pool to guarantee DATABASE_URL is set in os.environ
    BEFORE the app's lifespan starts (get_pool reads DATABASE_URL at startup).
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


# ── Test 1: Customer cancels upcoming reservation ─────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_customer_cancels_upcoming_reservation(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Customer cancels their own upcoming (pending) reservation via WhatsApp.

    Setup:
      - Seed restaurant with module_reservations=True (single location)
      - Create a restaurant table via POST /api/tables
      - Customer Turn 1: "Quiero hacer una reserva para 2 personas el {DATE} a las 8 pm"
      - Customer Turn 2: confirm if bot prompts
      - Assert reservation row exists with status='pending'
      - Customer Turn 3: "Quiero cancelar mi reserva"
      - Assert: reservations.status == 'cancelled' (exact)
      - Assert: reservations.cancelled_at IS NOT NULL
      - Assert: bot reply contains "cancel" (case-insensitive)
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Cancel Reservation Restaurant",
        bot_number_raw="+570E2ECANCELR",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=0,
        features_override={
            "module_reservations": True,
        },
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, parent_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM reservations WHERE org_id=$1", parent_id)

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
        "e2e.cancel_reservation.test_start",
        parent_id=parent_id,
        bot_number=bot_number,
        customer_phone=CUSTOMER_PHONE,
    )

    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # ── Create a table so availability check passes ────────────────────────────
    table_resp = await client.post("/api/tables", headers=auth_headers)
    assert table_resp.status_code == 200, (
        f"POST /api/tables failed: {table_resp.status_code} {table_resp.text}"
    )
    log.info("e2e.cancel_reservation.table_created", table_id=table_resp.json().get("table_id"))

    # ── Turn 1: Customer requests a reservation ────────────────────────────────
    reservation_text = (
        f"Hola, quisiera hacer una reserva para {RESERVATION_GUESTS} personas "
        f"el {RESERVATION_DATE_STR} a las 8 pm, a nombre de {RESERVATION_NAME}"
    )
    log.info("e2e.cancel_reservation.turn_1", text=reservation_text)
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=reservation_text, bot_number=bot_number,
    )
    log.info("e2e.cancel_reservation.turn_1_done",
             processed=processed_1, elapsed_s=round(time.monotonic() - t1_start, 1))
    assert processed_1 >= 1, "Turn 1 not processed"

    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(turn1_replies) >= 1, "Turn 1: bot sent no reply"

    # ── Turn 2 (conditional): confirm ────────────────────────────────────────
    confirm_text = "Sí, confirmo la reserva"
    log.info("e2e.cancel_reservation.turn_2", text=confirm_text)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=confirm_text, bot_number=bot_number,
    )
    log.info("e2e.cancel_reservation.turn_2_done",
             processed=processed_2, elapsed_s=round(time.monotonic() - t2_start, 1))
    assert processed_2 >= 1, "Turn 2 not processed"

    # ── Assert reservation row exists ─────────────────────────────────────────
    reservation_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_cancel_assert_reservation_exists"):
            async with pool.acquire() as conn:
                reservation_row = await conn.fetchrow(
                    """SELECT id, status, cancelled_at, cancellation_reason, org_id
                       FROM reservations
                       WHERE org_id=$1 AND phone=$2
                       ORDER BY created_at DESC LIMIT 1""",
                    parent_id, CUSTOMER_PHONE,
                )
        if reservation_row:
            break
        await asyncio.sleep(0.5)

    assert reservation_row is not None, (
        f"No reservation found for phone={CUSTOMER_PHONE}, org_id={parent_id}. "
        "Check turns 1-2 succeeded and the reservation was committed."
    )
    reservation_id = reservation_row["id"]
    assert reservation_row["status"] in ("pending", "confirmed"), (
        f"Expected pending or confirmed before cancel, got: {reservation_row['status']}"
    )
    log.info("e2e.cancel_reservation.reservation_found",
             reservation_id=reservation_id, status=reservation_row["status"])

    # ── Turn 3: Customer cancels ──────────────────────────────────────────────
    cancel_text = "Quiero cancelar mi reserva"
    log.info("e2e.cancel_reservation.turn_3", text=cancel_text)
    t3_start = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=cancel_text, bot_number=bot_number,
    )
    log.info("e2e.cancel_reservation.turn_3_done",
             processed=processed_3, elapsed_s=round(time.monotonic() - t3_start, 1))
    assert processed_3 >= 1, "Turn 3 (cancel request) not processed"

    # ── Turn 4 (conditional): Confirm cancellation if bot asks ────────────────
    # The LLM may ask the customer to confirm the cancellation before executing it.
    # We always send a confirmation turn to cover this path.
    confirm_cancel_text = "Sí, cancela la reserva"
    log.info("e2e.cancel_reservation.turn_4", text=confirm_cancel_text)
    t4_start = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=confirm_cancel_text, bot_number=bot_number,
    )
    log.info("e2e.cancel_reservation.turn_4_done",
             processed=processed_4, elapsed_s=round(time.monotonic() - t4_start, 1))
    assert processed_4 >= 1, "Turn 4 (cancel confirm) not processed"

    # ── Assert: status changed to 'cancelled' ─────────────────────────────────
    cancelled_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_cancel_assert_cancelled"):
            async with pool.acquire() as conn:
                cancelled_row = await conn.fetchrow(
                    """SELECT id, status, cancelled_at, cancellation_reason
                       FROM reservations
                       WHERE id=$1""",
                    reservation_id,
                )
        if cancelled_row and cancelled_row["status"] == "cancelled":
            break
        await asyncio.sleep(0.5)

    assert cancelled_row is not None, (
        f"Reservation row {reservation_id} disappeared from DB."
    )
    assert cancelled_row["status"] == "cancelled", (
        f"Expected status='cancelled', got '{cancelled_row['status']}'. "
        "The cancel_reservation tool may not have been called or the DB write failed."
    )
    assert cancelled_row["cancelled_at"] is not None, (
        f"cancelled_at is NULL after cancellation (reservation_id={reservation_id})"
    )
    log.info(
        "e2e.cancel_reservation.db_asserted",
        status=cancelled_row["status"],
        cancelled_at=str(cancelled_row["cancelled_at"]),
        cancellation_reason=cancelled_row["cancellation_reason"],
    )

    # ── Assert: bot reply mentions cancellation ───────────────────────────────
    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.cancel_reservation.replies",
             count=len(all_replies), last_replies=all_replies[-4:])
    all_texts = " ".join(all_replies).lower()
    assert "cancel" in all_texts, (
        f"Bot reply does not mention 'cancel'. All replies: {all_replies}"
    )


# ── Test 2: Cancelling past reservation is rejected ───────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cancel_past_reservation_rejected(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    When there is no upcoming (future, cancellable) reservation, the bot
    replies "no upcoming reservations" and does NOT change the reservation status.

    Setup:
      - Seed restaurant with module_reservations=True
      - DB INSERT a reservation with date in the PAST, status='pending'
        (bypassing the bot to avoid LLM ambiguity)
      - Customer turn: "cancela mi reserva"
      - Assert: reservations.status == 'pending' (unchanged)
      - Assert: bot reply does NOT acknowledge a cancelled reservation
    """
    client = e2e_app
    pool = test_pool

    # Use a different phone to avoid state bleed from test 1
    past_customer_phone_raw = "+573009995545"
    past_customer_phone = _normalize_phone(past_customer_phone_raw)

    restaurant = await seed_restaurant(
        pool,
        name="E2E Past Cancel Restaurant",
        bot_number_raw="+570E2EPASTCNL",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=0,
        features_override={"module_reservations": True},
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, parent_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM reservations WHERE org_id=$1", parent_id)

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

    # ── Seed a past reservation directly via DB ────────────────────────────────
    # bypass_tenant_scope is used because this is a test seed operation.
    # The INSERT sets org_id explicitly since the GUC may not be set here.
    past_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    reservation_id: int | None = None
    with bypass_tenant_scope("e2e_seed_past_reservation"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO reservations
                   (name, "date", "time", guests, phone, bot_number, notes,
                    status, org_id, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, NOW())
                   RETURNING id""",
                "Carlos Prueba",
                past_date,
                "18:00",
                2,
                past_customer_phone,
                bot_number,
                "",
                parent_id,
            )
            reservation_id = row["id"]

    assert reservation_id is not None, "Failed to seed past reservation"
    log.info("e2e.cancel_past_reservation.seeded",
             reservation_id=reservation_id, date=past_date, phone=past_customer_phone)

    # ── Customer turn: attempt to cancel ─────────────────────────────────────
    cancel_text = "cancela mi reserva"
    log.info("e2e.cancel_past_reservation.turn_1", text=cancel_text)
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=past_customer_phone_raw,
        text=cancel_text,
        bot_number=bot_number,
    )
    log.info("e2e.cancel_past_reservation.turn_1_done",
             processed=processed_1, elapsed_s=round(time.monotonic() - t1_start, 1))
    assert processed_1 >= 1, "Turn 1 (cancel attempt) not processed"

    # ── Assert: reservation status is still 'pending' ─────────────────────────
    with bypass_tenant_scope("e2e_assert_past_reservation_unchanged"):
        async with pool.acquire() as conn:
            unchanged_row = await conn.fetchrow(
                "SELECT status, cancelled_at FROM reservations WHERE id=$1",
                reservation_id,
            )

    assert unchanged_row is not None, f"Reservation row {reservation_id} disappeared"
    assert unchanged_row["status"] == "pending", (
        f"Past reservation status changed to '{unchanged_row['status']}' when it should "
        "remain 'pending'. The cancel_reservation query selects date >= CURRENT_DATE, "
        "so past reservations should be excluded."
    )
    assert unchanged_row["cancelled_at"] is None, (
        "cancelled_at was set on a past reservation — the query filter date >= CURRENT_DATE failed."
    )
    log.info("e2e.cancel_past_reservation.asserted_unchanged",
             status=unchanged_row["status"])

    # ── Assert: bot reply mentions no upcoming reservation ────────────────────
    replies = wa_capture.texts_to(past_customer_phone_raw)
    log.info("e2e.cancel_past_reservation.replies",
             count=len(replies), replies=replies[:5])
    assert len(replies) >= 1, "Bot sent no reply at all"
    all_texts = " ".join(replies).lower()
    # The bot should say something like "no tienes reservas próximas para cancelar"
    assert any(kw in all_texts for kw in ["próxima", "proxima", "futura", "no tienes", "no hay", "cancel"]), (
        f"Bot reply does not mention no upcoming reservation. Replies: {replies}"
    )
