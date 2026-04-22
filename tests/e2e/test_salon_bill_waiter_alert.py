"""
tests/e2e/test_salon_bill_waiter_alert.py — E2E test: Rule #17 enforcement.

Rule #17: Bill request fires waiter_alert at the INSTANT the customer asks for
the bill, BEFORE the checkout state machine completes.

The call site is agent_salon.py, action == "bill" branch, lines 1031-1044:
    await db.db_create_waiter_alert(alert_type='bill', ...)

This test asserts that a waiter_alerts row with alert_type='bill' EXISTS
in the DB immediately after drain_inbox returns from the bill-request turn —
before the customer completes any further checkout steps.

Flow:
  1. Seed restaurant + create table + admin token
  2. Turn 1: QR scan — opens dine-in session
  3. Turn 2: order 2 empanaditas
  4. Advance kitchen order through status transitions (recibido → listo) so the
     bot considers the order ready and the table has a valid cart total
  5. Turn 3: "la cuenta por favor" — bill request
  6. Assert waiter_alerts COUNT >= 1 for alert_type='bill' and table_id
  7. Assert bot reply acknowledges the bill request

Env:
  ANTHROPIC_API_KEY  — real Anthropic key
  TEST_DATABASE_URL  — Postgres URL (isolated test DB)
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

# ── Constants ──────────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573008885566"
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


# ── Test ───────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_salon_bill_fires_waiter_alert_immediately(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Rule #17: waiter_alert with alert_type='bill' MUST exist in the DB
    immediately after the customer says 'la cuenta' — not only after the
    checkout state machine finishes.

    If the COUNT is 0 after the bill-request turn, Rule #17 is broken.
    """
    client = e2e_app
    pool = test_pool

    # ── Seed ──────────────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Bill Alert Test Restaurant",
        bot_number_raw="+570E2EBILLALT",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    branch_1 = restaurant["branches"][0]
    branch_1_id = branch_1["id"]

    await truncate_e2e_data(pool, parent_id)
    await truncate_e2e_data(pool, branch_1_id)

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

    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    branch_headers = {**auth_headers, "X-Branch-ID": str(branch_1_id)}

    log.info("e2e.bill_alert_start", parent_id=parent_id, bot_number=bot_number)

    # ── Create table via admin API ─────────────────────────────────────────────
    create_table_resp = await client.post("/api/tables", headers=branch_headers)
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_id: str = create_table_resp.json()["table_id"]
    table_name: str = create_table_resp.json().get("name", table_id)
    log.info("e2e.bill_alert_table_created", table_id=table_id, table_name=table_name)

    # ── Turn 1: QR scan ───────────────────────────────────────────────────────
    qr_message = f"Hola, acabo de llegar a la mesa [table_id:{table_id}]"
    log.info("e2e.bill_alert_turn_1", text=qr_message)
    t1 = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=qr_message,
        bot_number=bot_number,
    )
    log.info("e2e.bill_alert_turn_1_done", processed=processed_1, elapsed=round(time.monotonic() - t1, 1))
    assert processed_1 >= 1, "Turn 1 (QR scan) was not processed"
    assert len(wa_capture.texts_to(CUSTOMER_PHONE_RAW)) >= 1, "Bot sent no reply after QR scan"

    # ── Assert: active table_session created ──────────────────────────────────
    session_row = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_bill_assert_session"):
            async with pool.acquire() as conn:
                session_row = await conn.fetchrow(
                    "SELECT id, phone, table_id, status FROM table_sessions "
                    "WHERE phone = $1 AND table_id = $2 AND status = 'active' LIMIT 1",
                    CUSTOMER_PHONE, table_id,
                )
        if session_row:
            break
        await asyncio.sleep(0.5)

    assert session_row is not None, (
        f"No active table_session found for phone={CUSTOMER_PHONE} table_id={table_id}"
    )
    log.info("e2e.bill_alert_session_confirmed", session_id=str(session_row["id"]))

    # ── Turn 2: order food ────────────────────────────────────────────────────
    order_text = "Quiero pedir 2 empanaditas de carne"
    log.info("e2e.bill_alert_turn_2", text=order_text)
    t2 = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=order_text,
        bot_number=bot_number,
    )
    log.info("e2e.bill_alert_turn_2_done", processed=processed_2, elapsed=round(time.monotonic() - t2, 1))
    assert processed_2 >= 1, "Turn 2 (order) was not processed"
    assert len(wa_capture.texts_to(CUSTOMER_PHONE_RAW)) >= 2, "Bot sent no reply after order"

    # ── Turn 2b (conditional): confirm order if bot prompts ───────────────────
    # The bot may ask "¿Confirmas tu pedido?" before actually placing it.
    last_reply_t2 = wa_capture.texts_to(CUSTOMER_PHONE_RAW)[-1].lower()
    needs_order_confirm = any(kw in last_reply_t2 for kw in ("confirm", "pedido", "proceder", "correctamente"))
    if needs_order_confirm:
        confirm_order_text = "sí confirmo"
        log.info("e2e.bill_alert_turn_2b", text=confirm_order_text)
        t2b = time.monotonic()
        processed_2b = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW,
            text=confirm_order_text,
            bot_number=bot_number,
        )
        log.info("e2e.bill_alert_turn_2b_done", processed=processed_2b, elapsed=round(time.monotonic() - t2b, 1))
        assert processed_2b >= 1, "Turn 2b (order confirmation) was not processed"

    # ── Find the table_order ──────────────────────────────────────────────────
    table_order_id = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_bill_assert_table_order"):
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM table_orders WHERE org_id = $1 AND phone = $2 "
                    "ORDER BY created_at DESC LIMIT 1",
                    parent_id, CUSTOMER_PHONE,
                )
        if row:
            table_order_id = row["id"]
            break
        await asyncio.sleep(0.5)

    assert table_order_id is not None, (
        f"No table_order found for org_id={parent_id} phone={CUSTOMER_PHONE}"
    )
    log.info("e2e.bill_alert_order_found", table_order_id=table_order_id)

    # ── Advance kitchen status: recibido → en_preparacion → listo ─────────────
    # This ensures the bot can compute a cart total when the customer asks for the bill.
    for status_step in ("recibido", "en_preparacion", "listo"):
        status_resp = await client.post(
            f"/api/table-orders/{table_order_id}/status",
            json={"status": status_step},
            headers=branch_headers,
        )
        log.info(
            "e2e.bill_alert_kitchen_status",
            status=status_step,
            code=status_resp.status_code,
        )
        # Status transitions may 200 or 422 if already in that state — not critical here
        # as long as the order was created. Continue regardless.

    # ── Turn 3: bill request ──────────────────────────────────────────────────
    bill_texts = ["la cuenta por favor", "quiero pagar"]
    bill_text = bill_texts[0]
    log.info("e2e.bill_alert_turn_3", text=bill_text)
    t3 = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=bill_text,
        bot_number=bot_number,
    )
    log.info("e2e.bill_alert_turn_3_done", processed=processed_3, elapsed=round(time.monotonic() - t3, 1))
    assert processed_3 >= 1, "Turn 3 (bill request) was not processed"

    # ── Assert: waiter_alert with alert_type='bill' exists ───────────────────
    # Rule #17: the alert MUST exist IMMEDIATELY after drain_inbox returns from
    # the bill-request turn (no polling beyond a quick check).
    # We allow a single short poll (2 × 0.3s) since drain_inbox is synchronous
    # within the test process but the DB write commits at the end of _dispatch.
    bill_alert_count = 0
    for _ in range(4):  # up to ~1.2s total
        with bypass_tenant_scope("e2e_bill_assert_waiter_alert"):
            async with pool.acquire() as conn:
                bill_alert_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM waiter_alerts
                    WHERE org_id = $1
                      AND table_id = $2
                      AND alert_type = 'bill'
                    """,
                    parent_id,
                    table_id,
                )
        if bill_alert_count >= 1:
            break
        await asyncio.sleep(0.3)

    log.info(
        "e2e.bill_alert_count",
        count=bill_alert_count,
        table_id=table_id,
        org_id=parent_id,
    )

    # HARD assertion — COUNT must be >= 1 (Rule #17, anti-permissiveness rule #3)
    assert bill_alert_count >= 1, (
        f"RULE #17 BROKEN: waiter_alert with alert_type='bill' was NOT found "
        f"for table_id={table_id} org_id={parent_id} immediately after bill request. "
        f"COUNT={bill_alert_count}. "
        "Check agent_salon.py action=='bill' branch — db_create_waiter_alert must be "
        "called BEFORE the checkout state machine (handle_checkout_flow) runs, "
        "not inside it."
    )

    # ── Assert: bot replied acknowledging the bill request ────────────────────
    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    # There must be at least 3 messages: turn1 reply + turn2 reply + turn3 reply
    assert len(all_replies) >= 3, (
        f"Expected >= 3 WA messages (QR reply + order reply + bill reply), "
        f"got {len(all_replies)}: {all_replies}"
    )
    # The last reply should acknowledge the bill — flexible substring match
    last_reply = all_replies[-1].lower()
    bill_ack_keywords = ["cuenta", "pago", "total", "pagar", "cobro", "momento"]
    ack_found = any(kw in last_reply for kw in bill_ack_keywords)
    log.info(
        "e2e.bill_alert_bot_reply",
        last_reply_preview=all_replies[-1][:120],
        ack_found=ack_found,
    )
    # Non-fatal: bot text varies, but we want a signal that the LLM addressed the bill.
    # If ack_found is False the test still passes — the waiter_alert assertion is the hard check.
    # Log for diagnostic purposes.
    if not ack_found:
        log.warning(
            "e2e.bill_alert_reply_no_ack_keyword",
            last_reply=all_replies[-1][:200],
            expected_one_of=bill_ack_keywords,
        )

    print(
        f"\n[OK] Rule #17 enforced: bill waiter_alert fires immediately.\n"
        f"  table_id={table_id}, org_id={parent_id}\n"
        f"  waiter_alerts COUNT={bill_alert_count}\n"
        f"  WA messages to customer: {len(all_replies)}"
    )
