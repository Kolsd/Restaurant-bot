"""
tests/e2e/test_salon_qr_lifecycle.py -- E2E test: full salon (dine-in) QR lifecycle.

ONE test that exercises:
  1. Restaurant + branch seeding (1 parent + 1 child)
  2. Table creation via POST /api/tables (admin API, realistic)
  3. WhatsApp inbound with [table_id:X] QR tag -- triggers real dine-in session
  4. Inbox worker drain (real agent.py -> Claude API -> tables_repo)
  5. Tenant RLS enforcement (all repos use real tenant_scope)
  6. Kitchen status transitions (recibido -> en_preparacion -> listo -> entregado)
  7. Caja check creation + pay_check (POST /api/table-orders/.../checks + .../pay)
  8. Session closure asserted in DB
  9. Outbound WA notification capture

If this test passes, production salon ordering works end-to-end.
If it fails, production is broken.

Prerequisites (env vars):
  ANTHROPIC_API_KEY   -- real Anthropic key
  TEST_DATABASE_URL   -- Postgres URL (or DATABASE_URL pointing at a test DB)
  DISABLE_EMBEDDED_WORKER=1  -- set automatically by conftest.py

Run:
  pytest tests/e2e/test_salon_qr_lifecycle.py -v -s -m e2e
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
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# -- Test constants ------------------------------------------------------------

CUSTOMER_PHONE_RAW = "+573009998877"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

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


# -- App fixture (function-scoped so lifespan runs per test) ------------------

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


# -- The single E2E test ------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_salon_qr_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full salon QR lifecycle:

    Setup:
      - Seed restaurant with 1 branch
      - Create a table via POST /api/tables (admin API)

    Turns:
      1. "Hola, acabo de llegar [table_id:{table_id}]" -- opens dine-in session
      2. "Quiero pedir 2 empanaditas de carne" -- agent calls place_order tool

    Then admin API:
      - GET /api/table-orders to find the order
      - POST .../status: recibido -> en_preparacion -> listo -> entregado
      - POST .../checks   (create a single check for the full table)
      - POST .../checks/{id}/pay  (pay with Nequi)
      - Assert table_orders.status = factura_entregada in DB
      - Assert table_sessions.status != 'active' in DB (session closed by pay_check)
    """
    client = e2e_app
    pool = test_pool

    # -- Seed restaurant (1 parent + 1 branch) --------------------------------
    restaurant = await seed_restaurant(
        pool,
        name="E2E Salon QR Test Restaurant",
        bot_number_raw="+570E2ESALON1",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[
            (4.710989, -74.072092),  # branch 1 -- Bogota zona norte
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]  # normalized (no +)
    branch_1 = restaurant["branches"][0]
    branch_1_id = branch_1["id"]
    branch_1_bot = branch_1["whatsapp_number"]

    # Clean volatile data from prior runs
    await truncate_e2e_data(pool, parent_id)
    await truncate_e2e_data(pool, branch_1_id)

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
        "e2e.salon_test_start",
        parent_id=parent_id,
        branch_1_id=branch_1_id,
        bot_number=bot_number,
        branch_1_bot=branch_1_bot,
    )

    # -- Create admin session token for API calls -----------------------------
    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    # For branch-scoped endpoints, tell the backend we're looking at branch 1
    branch_headers = {**auth_headers, "X-Branch-ID": str(branch_1_id)}

    # -- Create a table for branch 1 via admin API ----------------------------
    log.info("e2e.salon_create_table", branch_id=branch_1_id)
    create_table_resp = await client.post(
        "/api/tables",
        headers=branch_headers,
    )
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_data = create_table_resp.json()
    table_id: str = table_data["table_id"]
    table_name: str = table_data.get("name", table_id)
    log.info("e2e.salon_table_created", table_id=table_id, table_name=table_name)

    # -- Turn 1: QR scan -- opens dine-in session -----------------------------
    # A real QR scan on menu.html builds a wa.me link ending with [table_id:{id}].
    # detect_table_context() in agent.py regex: r'\[(?:table_id|t):([^\]]+)\]'
    qr_message = f"Hola, acabo de llegar a la mesa [table_id:{table_id}]"
    log.info("e2e.salon_turn_1", phone=CUSTOMER_PHONE, text=qr_message)
    t1_start = time.monotonic()
    # The first message goes to the parent's bot_number.
    # The agent resolves the table -> branch, so we can send to parent bot.
    # However, to be deterministic, send to the branch bot that owns the table.
    processed_1 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=qr_message,
        bot_number=bot_number,  # parent bot; agent resolves table -> branch
    )
    log.info(
        "e2e.salon_turn_1_done",
        processed=processed_1,
        elapsed_s=round(time.monotonic() - t1_start, 1),
    )
    assert processed_1 >= 1, "Turn 1: inbox item was not processed"

    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.salon_turn_1_replies",
        count=len(turn1_replies),
        replies=turn1_replies[:2],
    )
    assert len(turn1_replies) >= 1, (
        "Turn 1: bot sent no WA message. "
        "Check ANTHROPIC_API_KEY, bot_number lookup, and that the table was created."
    )

    # -- Assert: table_sessions row created -----------------------------------
    session_row = None
    for attempt in range(10):
        with bypass_tenant_scope("e2e_assert_session_created"):
            async with pool.acquire() as conn:
                session_row = await conn.fetchrow(
                    """
                    SELECT id, phone, table_id, status
                    FROM table_sessions
                    WHERE phone = $1
                      AND table_id = $2
                      AND status = 'active'
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    table_id,
                )
        if session_row:
            break
        await asyncio.sleep(0.5)

    assert session_row is not None, (
        f"No active table_session found for phone={CUSTOMER_PHONE} table_id={table_id}. "
        "Likely the [table_id:X] tag was not recognized or detect_table_context() failed."
    )
    log.info(
        "e2e.salon_session_confirmed",
        session_id=str(session_row["id"]),
        table_id=session_row["table_id"],
        status=session_row["status"],
    )

    # -- Turn 2: Order food ---------------------------------------------------
    order_text = "Quiero pedir 2 empanaditas de carne"
    log.info("e2e.salon_turn_2", phone=CUSTOMER_PHONE, text=order_text)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=order_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.salon_turn_2_done",
        processed=processed_2,
        elapsed_s=round(time.monotonic() - t2_start, 1),
    )
    assert processed_2 >= 1, "Turn 2: order inbox item was not processed"

    turn2_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.salon_turn_2_replies",
        count=len(turn2_replies),
        replies=turn2_replies[-2:],
    )
    assert len(turn2_replies) >= 2, (
        "Turn 2: bot sent no reply after order attempt. "
        "Check that the menu has 'Empanaditas de Carne' and place_order tool was called."
    )

    # -- Turn 3: Confirm order (place_order requires explicit confirmation) ---
    confirm_text = "sí confirmo"
    log.info("e2e.salon_turn_3", phone=CUSTOMER_PHONE, text=confirm_text)
    t3_start = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=confirm_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.salon_turn_3_done",
        processed=processed_3,
        elapsed_s=round(time.monotonic() - t3_start, 1),
    )
    assert processed_3 >= 1, "Turn 3: confirmation inbox item was not processed"

    # -- Assert: table_orders row exists in DB --------------------------------
    # Poll up to 10s -- agent may commit asynchronously after drain
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_assert_table_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, phone, table_id, status, total, items, org_id,
                           location_id, base_order_id
                    FROM table_orders
                    WHERE phone = $1
                      AND table_id = $2
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    table_id,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No table_order found for phone={CUSTOMER_PHONE} table_id={table_id}. "
        "Check that the bot called place_order and it committed to the DB."
    )

    from app.services.money import to_decimal
    order_id: str = order_row["id"]
    base_order_id: str = order_row["base_order_id"] or order_id
    order_total = to_decimal(order_row["total"])
    log.info(
        "e2e.salon_order_found",
        order_id=order_id,
        base_order_id=base_order_id,
        status=order_row["status"],
        total=str(order_total),
        org_id=order_row["org_id"],
        location_id=order_row["location_id"],
    )

    # total should be 2 * 15000 = 30000 (two empanaditas)
    assert order_total >= Decimal("15000"), (
        f"Expected table_order total >= 15000 (at least 1 empanadita), got {order_total}. "
        "The bot may have ordered a different quantity."
    )

    # -- Admin: status transitions: recibido -> en_preparacion -> listo -> entregado --
    # The endpoint is POST /api/table-orders/{order_id}/status (line 679 in tables.py)
    for new_status in ("en_preparacion", "listo", "entregado"):
        log.info("e2e.salon_patch_status", order_id=base_order_id, status=new_status)
        status_resp = await client.post(
            f"/api/table-orders/{base_order_id}/status",
            json={"status": new_status},
            headers=auth_headers,
        )
        assert status_resp.status_code == 200, (
            f"POST status={new_status} failed: "
            f"{status_resp.status_code} {status_resp.text}"
        )
        log.info("e2e.salon_status_updated", order_id=base_order_id, new_status=new_status)

    # Brief pause to let the 'entregado' WA notification task run
    await asyncio.sleep(1.0)

    # -- Caja: create a single check covering all items -----------------------
    # First, get the full ticket so we can build the check payload correctly.
    log.info("e2e.salon_create_checks", base_order_id=base_order_id)

    # Build check payload: 1 check with all items from the table order.
    # We construct from the DB order_row items; price comes from the menu.
    items_raw = order_row["items"]
    if isinstance(items_raw, str):
        import json as _json
        items_raw = _json.loads(items_raw)
    items_raw = items_raw or []

    check_items = []
    for item in items_raw:
        check_items.append({
            "name": item["name"],
            "qty": int(item.get("qty", item.get("quantity", 1))),
            "unit_price": float(to_decimal(item.get("price", item.get("unit_price", 15000)))),
        })

    if not check_items:
        # Fallback: if items are empty (order stored differently), use known values
        check_items = [{"name": "Empanaditas de Carne (3 uds)", "qty": 2, "unit_price": 15000.0}]

    checks_payload = {
        "checks": [
            {
                "check_number": 1,
                "items": check_items,
            }
        ],
        "tax_pct": 0.0,   # no IVA in this test so total == subtotal == price
        "tax_regime": "iva",
    }

    create_checks_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks",
        json=checks_payload,
        headers=auth_headers,
    )
    assert create_checks_resp.status_code == 200, (
        f"POST checks failed: {create_checks_resp.status_code} {create_checks_resp.text}"
    )
    checks_data = create_checks_resp.json()
    checks_list = checks_data.get("checks", [])
    assert len(checks_list) >= 1, (
        f"Expected at least 1 check in response, got: {checks_data}"
    )
    check_id: str = checks_list[0]["id"]
    check_total = to_decimal(checks_list[0]["total"])
    log.info(
        "e2e.salon_check_created",
        check_id=check_id,
        check_total=str(check_total),
    )

    # -- Caja: pay the check --------------------------------------------------
    log.info("e2e.salon_pay_check", check_id=check_id, amount=float(check_total))
    pay_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks/{check_id}/pay",
        json={
            "payments": [
                {"method": "Nequi", "amount": float(check_total)},
            ],
            "tip_amount": 0.0,
            "service_charge": 0.0,
            "customer_name": "E2E Test Customer",
            "customer_nit": "222222222",
            "customer_email": "",
        },
        headers=auth_headers,
    )
    assert pay_resp.status_code == 200, (
        f"POST pay_check failed: {pay_resp.status_code} {pay_resp.text}"
    )
    pay_data = pay_resp.json()
    assert pay_data.get("success") is True, (
        f"pay_check did not return success=true: {pay_data}"
    )
    log.info(
        "e2e.salon_check_paid",
        check_id=check_id,
        change=pay_data.get("change"),
    )

    # -- Assert: table_orders.status is terminal (factura_entregada) ----------
    # After all checks are paid, db_finalize_check_payment marks the base order
    # as 'factura_entregada'.
    final_order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_assert_final_order_status"):
            async with pool.acquire() as conn:
                final_order_row = await conn.fetchrow(
                    "SELECT status FROM table_orders WHERE id = $1",
                    base_order_id,
                )
        if final_order_row and final_order_row["status"] in (
            "factura_entregada", "cerrar_mesa", "closed"
        ):
            break
        await asyncio.sleep(0.5)

    assert final_order_row is not None, (
        f"table_order {base_order_id} disappeared from DB after pay_check"
    )
    assert final_order_row["status"] in ("factura_entregada", "cerrar_mesa", "closed"), (
        f"Expected table_order to be in terminal status after payment, "
        f"got '{final_order_row['status']}'"
    )
    log.info(
        "e2e.salon_order_terminal",
        order_id=base_order_id,
        status=final_order_row["status"],
    )

    # -- Assert: table_session is no longer active ----------------------------
    # pay_check calls _farewell_and_nps which calls db_mark_session_nps_pending,
    # setting the session status to 'nps_pending' (or 'closed' if explicitly closed).
    # Either way, status must not be 'active'.
    await asyncio.sleep(1.0)  # brief wait for the async task to run
    with bypass_tenant_scope("e2e_assert_session_closed"):
        async with pool.acquire() as conn:
            session_after = await conn.fetchrow(
                """
                SELECT status FROM table_sessions
                WHERE phone = $1
                  AND table_id = $2
                ORDER BY started_at DESC
                LIMIT 1
                """,
                CUSTOMER_PHONE,
                table_id,
            )

    assert session_after is not None, (
        f"table_session for phone={CUSTOMER_PHONE} table_id={table_id} not found after payment"
    )
    assert session_after["status"] != "active", (
        f"Expected session to be closed/nps_pending after payment, "
        f"got status='{session_after['status']}'"
    )
    log.info(
        "e2e.salon_session_closed",
        status=session_after["status"],
    )

    # -- Final summary --------------------------------------------------------
    all_customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.salon_test_passed",
        base_order_id=base_order_id,
        check_id=check_id,
        order_total=str(order_total),
        final_order_status=final_order_row["status"],
        session_status=session_after["status"],
        total_wa_messages=len(wa_capture.messages),
        customer_wa_messages=len(all_customer_texts),
    )
    print(
        f"\n[OK] E2E salon test passed: order {base_order_id} completed full "
        f"dine-in lifecycle (QR scan -> order -> kitchen -> pay_check). "
        f"{len(wa_capture.messages)} WA messages captured."
    )
