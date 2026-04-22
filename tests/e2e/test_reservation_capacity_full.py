"""
tests/e2e/test_reservation_capacity_full.py — E2E test: reservation rejected when no capacity.

ONE test that exercises:
  1. Restaurant seeding with module_reservations=True (single location)
  2. Table creation via POST /api/tables (capacity=4, default)
  3. Direct DB INSERT of a confirmed reservation on that table for TOMORROW 19:00
     — this blocks the only slot
  4. Customer WhatsApp inbound requesting a reservation for 4 persons, same date/time
  5. Inbox worker drain (real agent.py -> Claude API -> reservations_repo)
  6. Assert: bot reply contains an unavailability phrase
  7. Assert: no new reservation row was inserted for the customer phone
  8. Assert: the seeded (blocked) reservation remains unchanged

Why this test:
  agent.py calls db_get_available_tables(date, time, guests, bot_number) BEFORE
  inserting a reservation. When the only table is already booked within ±2h of the
  requested time, it returns [] and the bot appends an unavailability message
  WITHOUT creating a reservation row.

Prerequisites (env vars):
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — Postgres URL (dedicated test DB with alembic head applied)

Run:
  pytest tests/e2e/test_reservation_capacity_full.py -v -s -m e2e
"""
from __future__ import annotations

import asyncio
import time
import uuid
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

CUSTOMER_PHONE_RAW = "+573009991122"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# Use the day after tomorrow to reduce LLM ambiguity around "mañana".
# The blocked reservation is also seeded for this same date.
_DATE: date = date.today() + timedelta(days=1)
DATE_STR: str = _DATE.strftime("%Y-%m-%d")      # YYYY-MM-DD for DB INSERT
DATE_HUMAN: str = _DATE.strftime("%d/%m/%Y")    # compact format for bot message

RESERVATION_GUESTS: int = 4
SEEDED_PERSON_NAME: str = "Carlos Ruiz"         # the existing blocked reservation
CUSTOMER_NAME: str = "Ana Torres"               # the customer's request (should be rejected)

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

# Phrases the bot must include when no availability exists (from agent.py:1418-1423).
# At least one must appear in the combined reply text.
UNAVAILABILITY_PHRASES = [
    "no tenemos disponibilidad",
    "no hay mesas",
    "no disponible",
    "completamente llena",
    "ocupad",
    "no hay disponibilidad",
    "disponibles para esa",
]


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
async def test_reservation_capacity_full(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    When the only table is already confirmed-reserved for the requested date/time,
    the bot must:
      - Reply with an unavailability phrase
      - NOT insert a new reservation row for the customer
      - Leave the seeded (blocked) reservation intact
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant (single location, no branches needed) ─────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Reservation Capacity Test",
        bot_number_raw="+570E2ERESVFULL",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=0,
        features_override={
            "module_reservations": True,
        },
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    # Clean volatile state from prior runs
    await truncate_e2e_data(pool, org_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM reservations WHERE org_id = $1", org_id)

    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    # ── Create the only restaurant table ──────────────────────────────────────
    # First delete any tables left over from prior test runs so there is exactly
    # ONE table — otherwise a second run has 2 tables and the blocking reservation
    # only covers one of them, allowing the new reservation to slip through.
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    with bypass_tenant_scope("e2e_capacity_delete_old_tables"):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM restaurant_tables WHERE branch_id IN "
                "(SELECT id FROM locations WHERE org_id = $1)",
                org_id,
            )
    log.info("e2e.capacity.old_tables_deleted", org_id=org_id)

    create_table_resp = await client.post("/api/tables", headers=auth_headers)
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_data = create_table_resp.json()
    table_id: str = table_data["table_id"]
    log.info("e2e.capacity.table_created", table_id=table_id, org_id=org_id)

    # ── Resolve principal location_id (needed for restaurant_tables.branch_id) ─
    # db_get_available_tables queries: WHERE t.branch_id = $1 (the resolved location).
    # The table we created is attached to that principal location. We also need the
    # location_id to seed the reservation with the correct table_id.
    with bypass_tenant_scope("e2e_capacity_resolve_location"):
        async with pool.acquire() as conn:
            loc_row = await conn.fetchrow(
                """
                SELECT id FROM locations
                WHERE org_id = $1 AND whatsapp_number IS NULL
                ORDER BY id ASC LIMIT 1
                """,
                org_id,
            )
    assert loc_row is not None, f"Could not find principal location for org_id={org_id}"
    location_id = loc_row["id"]
    log.info("e2e.capacity.location_id", location_id=location_id)

    # ── Seed a CONFIRMED reservation that blocks the only table ───────────────
    # Status = 'confirmed' → included in the blocking sub-query of db_get_available_tables.
    # Time = "19:00" → falls within ±2h of the customer's request ("19:00").
    # The seeded row has table_id set so the NOT IN sub-query matches exactly.
    with bypass_tenant_scope("e2e_capacity_seed_reservation"):
        async with pool.acquire() as conn:
            seeded_row = await conn.fetchrow(
                """
                INSERT INTO reservations
                  (name, "date", "time", guests, phone, bot_number, status,
                   table_id, org_id)
                VALUES ($1, $2, $3, $4, $5, $6, 'confirmed', $7, $8)
                RETURNING id
                """,
                SEEDED_PERSON_NAME,
                DATE_STR,
                "19:00",
                RESERVATION_GUESTS,
                "573009990000",   # a different phone (not the test customer)
                bot_number,
                table_id,
                org_id,
            )
    seeded_reservation_id: int = seeded_row["id"]
    log.info(
        "e2e.capacity.seeded_reservation",
        seeded_id=seeded_reservation_id,
        date=DATE_STR,
        time="19:00",
        table_id=table_id,
    )

    # ── Turn 1: Customer requests a reservation for the same date/time ────────
    # Provide the explicit YYYY-MM-DD date so the LLM calls make_reservation
    # immediately without asking for clarification. "mañana" is ambiguous — the
    # bot asks to confirm the date before proceeding and never hits the capacity check.
    request_text = (
        f"Hola, quiero reservar una mesa para {RESERVATION_GUESTS} personas "
        f"el {DATE_STR} a las 19:00, a nombre de {CUSTOMER_NAME}"
    )
    log.info("e2e.capacity.turn_1", phone=CUSTOMER_PHONE, text=request_text)
    t1_start = time.monotonic()
    processed = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=request_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.capacity.turn_1_done",
        processed=processed,
        elapsed_s=round(time.monotonic() - t1_start, 1),
    )
    assert processed >= 1, "Turn 1: inbox item was not processed"

    # ── Assert: bot replied ────────────────────────────────────────────────────
    customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(customer_texts) >= 1, (
        "Bot sent no WA message. Check ANTHROPIC_API_KEY, bot_number lookup, "
        "and module_reservations feature flag."
    )

    # The bot may ask for confirmation before calling make_reservation.
    # Send a confirmation to trigger the actual tool call (which runs the
    # capacity check in agent.py). This mirrors the reservation_lifecycle test
    # which always sends a second "confirm" turn.
    combined_after_turn1 = " ".join(customer_texts).lower()
    needs_confirmation = (
        "confirmar" in combined_after_turn1
        or "confirma" in combined_after_turn1
        or "antes de" in combined_after_turn1
        or "¿confirmas" in combined_after_turn1
        or "¿todo correcto" in combined_after_turn1
    )
    if needs_confirmation or not any(p in combined_after_turn1 for p in UNAVAILABILITY_PHRASES):
        log.info("e2e.capacity.turn_2_confirm", reason="bot asked for confirmation or no unavailability phrase yet")
        proc_conf = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW,
            text="Sí, confirmo la reserva",
            bot_number=bot_number,
        )
        assert proc_conf >= 1, "Confirmation turn not processed"

    customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    combined_reply = " ".join(customer_texts).lower()
    log.info(
        "e2e.capacity.bot_reply",
        reply_count=len(customer_texts),
        combined_preview=combined_reply[:200],
    )

    # ── Assert: bot included an unavailability phrase ─────────────────────────
    matched_phrase = next(
        (p for p in UNAVAILABILITY_PHRASES if p in combined_reply),
        None,
    )
    assert matched_phrase is not None, (
        f"Bot reply did not contain any unavailability phrase. "
        f"Combined reply: {combined_reply[:400]!r}. "
        f"Expected one of: {UNAVAILABILITY_PHRASES}. "
        "Check agent.py reserve branch: the 'if not available' path appends the unavailability note."
    )
    log.info("e2e.capacity.unavailability_phrase_found", phrase=matched_phrase)

    # ── Assert: no new reservation was created for the customer ───────────────
    with bypass_tenant_scope("e2e_capacity_assert_no_new_reservation"):
        async with pool.acquire() as conn:
            new_res = await conn.fetchrow(
                """
                SELECT id, status FROM reservations
                WHERE org_id = $1 AND phone = $2 AND status = 'pending'
                ORDER BY id DESC LIMIT 1
                """,
                org_id,
                CUSTOMER_PHONE,
            )
    assert new_res is None, (
        f"A pending reservation was inserted for customer phone={CUSTOMER_PHONE} "
        f"even though the slot was fully booked. "
        f"Row found: id={new_res['id']}, status={new_res['status']}. "
        "The capacity check in agent.py may not have blocked the insertion."
    )
    log.info("e2e.capacity.no_new_reservation_confirmed")

    # ── Assert: seeded reservation is unchanged ───────────────────────────────
    with bypass_tenant_scope("e2e_capacity_assert_seeded_unchanged"):
        async with pool.acquire() as conn:
            seeded_final = await conn.fetchrow(
                "SELECT id, status, name FROM reservations WHERE id = $1",
                seeded_reservation_id,
            )
    assert seeded_final is not None, (
        f"Seeded reservation id={seeded_reservation_id} disappeared unexpectedly."
    )
    assert seeded_final["status"] == "confirmed", (
        f"Seeded reservation status changed from 'confirmed' to '{seeded_final['status']}'. "
        "Something unexpectedly mutated the existing reservation."
    )
    log.info(
        "e2e.capacity.seeded_reservation_intact",
        id=seeded_final["id"],
        status=seeded_final["status"],
    )

    print(
        f"\n[OK] Reservation capacity full test passed: "
        f"bot replied with unavailability phrase '{matched_phrase}', "
        f"no new reservation created, seeded reservation unchanged. "
        f"{len(wa_capture.messages)} WA messages captured."
    )
