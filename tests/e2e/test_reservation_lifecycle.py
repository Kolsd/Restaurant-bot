"""
tests/e2e/test_reservation_lifecycle.py — E2E test: full reservation lifecycle.

ONE test that exercises:
  1. Restaurant seeding with module_reservations=True (single location — no GPS needed)
  2. Restaurant table creation via POST /api/tables (needed for availability check)
  3. WhatsApp inbound: customer requests a reservation with guests/date/time/name
  4. Inbox worker drain (real agent.py -> Claude API -> reservations_repo)
  5. DB assertion: reservation row exists with correct guests, phone, org_id, status=pending
  6. Admin: confirm reservation via PUT /api/reservations/{id}/status (status=confirmed)
  7. DB assertion: status = confirmed
  8. Admin: mark no_show via PUT /api/reservations/{id}/status (status=no_show)
  9. DB assertion: status = no_show, no_show = TRUE
  10. Outbound WA message capture asserted (>= 1 message)

Why single location:
  The reserve action in agent.py calls db_get_available_tables(date, time, guests, bot_number)
  which resolves the location from the principal whatsapp_number and queries restaurant_tables
  for that location. Multi-branch GPS routing is not needed for reservation flow.

Why we create a table first:
  db_get_available_tables returns [] if no active restaurant_tables row exists for the
  resolved location with capacity >= guests. An empty result blocks reservation creation
  (the bot warns "no hay mesas disponibles" and returns without inserting a DB row).

Admin API endpoints used:
  POST /api/tables                          — create a table (sets up availability)
  GET  /api/reservations                    — list reservations to find created row
  PUT  /api/reservations/{id}/status        — confirm / no_show

Prerequisites (env vars):
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — Postgres URL (dedicated test DB with alembic head applied)

Run:
  pytest tests/e2e/test_reservation_lifecycle.py -v -s -m e2e
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

CUSTOMER_PHONE_RAW = "+573009997766"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# A concrete future date 7 days from today — deterministic for the LLM.
# Using a specific date (not "mañana") lets the LLM reliably format YYYY-MM-DD.
_RESERVATION_DATE: date = date.today() + timedelta(days=7)
RESERVATION_DATE_STR: str = _RESERVATION_DATE.strftime("%Y-%m-%d")
RESERVATION_DATE_HUMAN: str = _RESERVATION_DATE.strftime("%d de %B de %Y")  # e.g. "28 de abril de 2026"
RESERVATION_TIME: str = "19:00"
RESERVATION_GUESTS: int = 4
RESERVATION_NAME: str = "Andrea Gómez"

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


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app via ASGI transport.
    Lifespan is started and shut down cleanly. DISABLE_EMBEDDED_WORKER=1 prevents
    the background inbox loop from competing with drain_inbox().
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
async def test_reservation_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full reservation lifecycle:

    Setup:
      - Seed restaurant with module_reservations=True (single location)
      - Create a table via POST /api/tables (ensures availability check returns >= 1 table)

    Bot turns:
      1. Customer requests reservation: name + date + time + guests in one message
      2. (Conditional) Customer confirms if bot prompts for confirmation

    Admin flow:
      - GET /api/reservations to find the created row (by phone)
      - PUT /api/reservations/{id}/status  {"status": "confirmed"}  -> assert confirmed
      - PUT /api/reservations/{id}/status  {"status": "no_show"}    -> assert no_show

    Throughout:
      - DB assertions use bypass_tenant_scope (cross-tenant read in test context)
      - All status transitions are exact string matches (no OR-chains without justification)
      - WA message count asserted as >= 1 (LLM text varies; content is not asserted)
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant ─────────────────────────────────────────────────────────
    # num_branches=0: single sede only — reservations use the principal bot_number.
    # The availability query resolves location from whatsapp_number (principal),
    # so no branch locations are needed.
    restaurant = await seed_restaurant(
        pool,
        name="E2E Reservation Test Restaurant",
        bot_number_raw="+570E2ERESERV1",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=0,  # No branch locations — single sede
        features_override={
            "module_reservations": True,
            # reservation_auto_confirm intentionally OFF — tests admin-confirms path
            # reservation_deposits intentionally OFF — Wompi flow not covered here
        },
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]  # normalized (no +)
    owner_email = restaurant["owner_email"]

    # Clean volatile data from prior runs
    await truncate_e2e_data(pool, parent_id)
    # Also clean reservations for this org (not in truncate_e2e_data by default)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM reservations WHERE org_id = $1",
            parent_id,
        )

    # Reset in-process fallback state (NPS, checkout, cart locks)
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass  # non-critical — state_store module may not be imported yet

    log.info(
        "e2e.reservation_test_start",
        parent_id=parent_id,
        bot_number=bot_number,
        reservation_date=RESERVATION_DATE_STR,
        reservation_time=RESERVATION_TIME,
        guests=RESERVATION_GUESTS,
    )

    # ── Admin session token ────────────────────────────────────────────────────
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # ── Create a restaurant table (required for availability check) ─────────────
    # The reserve action in agent.py calls db_get_available_tables which queries
    # restaurant_tables WHERE branch_id = <principal_loc_id> AND active = TRUE
    # AND capacity >= guests. Without at least one such row, availability returns []
    # and the bot refuses to create the reservation.
    #
    # POST /api/tables (no X-Branch-ID header) uses restaurant["location_id"] as the
    # branch_id for the new table, which is the principal location's ID — the same
    # location that db_get_available_tables resolves via the principal bot_number.
    # The default capacity from migration 0014 is 4, satisfying >= RESERVATION_GUESTS (4).
    log.info("e2e.reservation_create_table", parent_id=parent_id)
    create_table_resp = await client.post(
        "/api/tables",
        headers=auth_headers,
    )
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_data = create_table_resp.json()
    table_id: str = table_data["table_id"]
    log.info("e2e.reservation_table_created", table_id=table_id)

    # ── Turn 1: Customer requests a reservation ────────────────────────────────
    # The message explicitly provides all required fields for the make_reservation tool:
    #   - name (Andrea Gómez)
    #   - date (RESERVATION_DATE_STR in human-readable form — LLM converts to YYYY-MM-DD)
    #   - time (7 pm -> LLM formats as 19:00)
    #   - guests (4 personas)
    # We include the specific date as "YYYY-MM-DD" directly to reduce LLM ambiguity.
    reservation_text = (
        f"Hola, quiero hacer una reserva para {RESERVATION_GUESTS} personas "
        f"el {RESERVATION_DATE_STR} a las 7 pm, a nombre de {RESERVATION_NAME}"
    )
    log.info("e2e.reservation_turn_1", phone=CUSTOMER_PHONE, text=reservation_text)
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=reservation_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.reservation_turn_1_done",
        processed=processed_1,
        elapsed_s=round(time.monotonic() - t1_start, 1),
    )
    assert processed_1 >= 1, "Turn 1: inbox item was not processed"

    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.reservation_turn_1_replies",
        count=len(turn1_replies),
        replies=turn1_replies[:3],
    )
    assert len(turn1_replies) >= 1, (
        "Turn 1: bot sent no WA message. "
        "Check ANTHROPIC_API_KEY, bot_number lookup, and module_reservations feature flag."
    )

    # ── Turn 2 (conditional): Confirm if the bot asks ─────────────────────────
    # The bot may (a) create the reservation immediately and confirm it,
    # or (b) ask the customer to confirm before proceeding.
    # We always send a confirmation turn — it's a no-op if the reservation was
    # already created (the bot will just reply conversationally).
    confirm_text = "Sí, confirmo la reserva"
    log.info("e2e.reservation_turn_2", phone=CUSTOMER_PHONE, text=confirm_text)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=confirm_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.reservation_turn_2_done",
        processed=processed_2,
        elapsed_s=round(time.monotonic() - t2_start, 1),
    )
    assert processed_2 >= 1, "Turn 2: confirmation inbox item was not processed"

    # ── Assert: reservation row exists in DB ───────────────────────────────────
    # Poll up to 10s (20 × 0.5s). The agent commits the row synchronously inside
    # _dispatch, so it should be visible immediately after drain_inbox returns.
    # We bypass RLS here because this is a test assertion (cross-tenant read).
    reservation_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_assert_reservation_created"):
            async with pool.acquire() as conn:
                reservation_row = await conn.fetchrow(
                    """
                    SELECT id, name, phone, guests, "date", "time",
                           status, org_id, no_show
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
        f"The bot may have returned 'no hay mesas disponibles' (table capacity mismatch), "
        f"or the make_reservation tool was not called. Check turn1_replies: {turn1_replies}"
    )

    reservation_id: int = reservation_row["id"]
    log.info(
        "e2e.reservation_row_found",
        reservation_id=reservation_id,
        name=reservation_row["name"],
        phone=reservation_row["phone"],
        guests=reservation_row["guests"],
        date=str(reservation_row["date"]),
        status=reservation_row["status"],
        org_id=reservation_row["org_id"],
    )

    # Exact assertions — no OR-chains for fields that must be deterministic
    assert reservation_row["guests"] == RESERVATION_GUESTS, (
        f"Expected guests={RESERVATION_GUESTS}, got {reservation_row['guests']}. "
        "The LLM may have misread the party size from the message."
    )
    # LLM name capture may strip accents or change case but must contain the
    # requested first name. Case-insensitive "andrea" is the minimum signal that
    # the right name was captured.
    assert "andrea" in reservation_row["name"].lower(), (
        f"Expected reservation name to contain 'Andrea' (case-insensitive), "
        f"got '{reservation_row['name']}'. "
        "The LLM may have captured a default or hallucinated a different name."
    )
    assert reservation_row["phone"] == CUSTOMER_PHONE, (
        f"Expected phone={CUSTOMER_PHONE}, got {reservation_row['phone']}. "
        "Phone normalization may have failed."
    )
    assert str(reservation_row["org_id"]) == str(parent_id), (
        f"Expected org_id={parent_id}, got {reservation_row['org_id']}. "
        "RLS GUC was not set correctly during reservation insert."
    )
    # Status must be 'pending' (reservation_auto_confirm is OFF; deposits are OFF).
    # 'pending' is the only valid initial status in this test configuration.
    assert reservation_row["status"] == "pending", (
        f"Expected status='pending' (auto_confirm=False, deposits=False), "
        f"got '{reservation_row['status']}'. "
        "If reservation_auto_confirm was accidentally enabled, check features_override."
    )

    # Critical: the LLM-captured date must match what the test requested.
    # This catches a common regression where the agent defaults to "hoy" or "mañana"
    # regardless of the user's actual date request.
    assert str(reservation_row["date"]) == RESERVATION_DATE_STR, (
        f"Expected reservation date={RESERVATION_DATE_STR}, got {reservation_row['date']}. "
        "The LLM may have misread or defaulted the date from the message."
    )
    # Time: the LLM converts "7 pm" → "19:00". DB column is TIME type (HH:MM:SS).
    # Accept either "19:00" or "19:00:00" since the time column coercion varies.
    # Both represent the same instant; this is a format-tolerance, not a value-tolerance.
    time_str = str(reservation_row["time"])
    assert time_str in ("19:00", "19:00:00"), (
        f"Expected reservation time=19:00 or 19:00:00, got '{time_str}'. "
        "The LLM may have misread the requested time."
    )

    # ── Admin: confirm the reservation ─────────────────────────────────────────
    log.info("e2e.reservation_admin_confirm", reservation_id=reservation_id)
    confirm_resp = await client.put(
        f"/api/reservations/{reservation_id}/status",
        json={"status": "confirmed"},
        headers=auth_headers,
    )
    assert confirm_resp.status_code == 200, (
        f"PUT /api/reservations/{reservation_id}/status (confirmed) failed: "
        f"{confirm_resp.status_code} {confirm_resp.text}"
    )
    confirm_data = confirm_resp.json()
    assert confirm_data.get("status") == "confirmed", (
        f"Response body did not have status='confirmed': {confirm_data}"
    )
    log.info("e2e.reservation_confirmed", reservation_id=reservation_id)

    # ── DB assert: status = confirmed ──────────────────────────────────────────
    with bypass_tenant_scope("e2e_assert_reservation_confirmed"):
        async with pool.acquire() as conn:
            confirmed_row = await conn.fetchrow(
                "SELECT status, confirmed_at FROM reservations WHERE id = $1",
                reservation_id,
            )
    assert confirmed_row is not None, (
        f"Reservation {reservation_id} disappeared after confirm"
    )
    assert confirmed_row["status"] == "confirmed", (
        f"DB status after admin confirm: expected 'confirmed', got '{confirmed_row['status']}'"
    )
    assert confirmed_row["confirmed_at"] is not None, (
        "confirmed_at should be set when status transitions to 'confirmed'"
    )
    log.info(
        "e2e.reservation_db_confirmed",
        reservation_id=reservation_id,
        confirmed_at=str(confirmed_row["confirmed_at"]),
    )

    # ── Admin: mark no_show ────────────────────────────────────────────────────
    log.info("e2e.reservation_admin_no_show", reservation_id=reservation_id)
    no_show_resp = await client.put(
        f"/api/reservations/{reservation_id}/status",
        json={"status": "no_show"},
        headers=auth_headers,
    )
    assert no_show_resp.status_code == 200, (
        f"PUT /api/reservations/{reservation_id}/status (no_show) failed: "
        f"{no_show_resp.status_code} {no_show_resp.text}"
    )
    no_show_data = no_show_resp.json()
    assert no_show_data.get("status") == "no_show", (
        f"Response body did not have status='no_show': {no_show_data}"
    )
    log.info("e2e.reservation_no_show_set", reservation_id=reservation_id)

    # ── DB assert: final status = no_show, no_show flag = TRUE ────────────────
    with bypass_tenant_scope("e2e_assert_reservation_no_show"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                "SELECT status, no_show FROM reservations WHERE id = $1",
                reservation_id,
            )
    assert final_row is not None, (
        f"Reservation {reservation_id} disappeared after no_show"
    )
    assert final_row["status"] == "no_show", (
        f"DB status after no_show: expected 'no_show', got '{final_row['status']}'"
    )
    assert final_row["no_show"] is True, (
        f"DB no_show flag should be TRUE after no_show transition, got {final_row['no_show']}"
    )
    log.info(
        "e2e.reservation_final_state",
        reservation_id=reservation_id,
        status=final_row["status"],
        no_show=final_row["no_show"],
    )

    # ── Final WA capture assertion ─────────────────────────────────────────────
    all_customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    # 2 turns drained → bot must have replied to both.
    # Per CLAUDE.md Rule #8: the LLM path never returns silence to the customer.
    # If count < 2, either drain processed a duplicate (dedup bug) or the bot
    # silently swallowed a turn.
    assert len(all_customer_texts) >= 2, (
        f"Expected >= 2 WA messages to customer (one per drained turn), "
        f"got {len(all_customer_texts)}. "
        f"Messages received: {all_customer_texts}. "
        "Possible bug: dedup false positive, LLM silent failure, or wa_capture miss."
    )
    log.info(
        "e2e.reservation_test_passed",
        reservation_id=reservation_id,
        final_status=final_row["status"],
        total_wa_messages=len(wa_capture.messages),
        customer_wa_messages=len(all_customer_texts),
        customer_texts_preview=[t[:80] for t in all_customer_texts],
    )
    print(
        f"\n[OK] E2E reservation test passed: reservation {reservation_id} "
        f"completed full lifecycle (bot capture -> admin confirm -> no_show). "
        f"{len(wa_capture.messages)} WA messages captured."
    )
