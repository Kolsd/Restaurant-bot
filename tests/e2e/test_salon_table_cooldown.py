"""
tests/e2e/test_salon_table_cooldown.py — E2E test: Rule #5 enforcement.

Rule #5: table_cooldown_acquire with 5-min TTL prevents a second customer from
opening the same table within the cooldown window.

The state key is:
    mesio:cooldown:table:{table_id}:{bot_number}

The flow in agent_salon.py / agent.py:
    acquired = await state_store.table_cooldown_acquire(table_id, bot_number, ...)
    if not acquired:
        → send "table occupied" message to customer B, do NOT create table_session

Flow:
  1. Seed restaurant + create 1 table
  2. Customer A sends [table_id:X] → session_A opens → cooldown acquired
  3. Customer B sends SAME [table_id:X] immediately (no sleep) → should be blocked
  4. Assert bot reply to B mentions table is occupied (flexible keyword match)
  5. Assert only ONE active table_session exists for that table_id

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

PHONE_A_RAW = "+573001234567"
PHONE_A = _normalize_phone(PHONE_A_RAW)

PHONE_B_RAW = "+573007654321"
PHONE_B = _normalize_phone(PHONE_B_RAW)

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

PAYMENT_METHODS = ["Efectivo"]

# Keywords that the bot should use when telling customer B the table is occupied.
# The bot text varies by LLM output; at least one of these must appear.
_OCCUPIED_KEYWORDS = ["ocupad", "ya está", "otro cliente", "en uso", "uso", "disponible"]


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
async def test_table_cooldown_blocks_second_customer(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Rule #5: table_cooldown_acquire prevents a second customer from opening the
    same table while the cooldown lock is held by the first customer.

    If two active sessions are created on the same table, Rule #5 is broken.
    """
    client = e2e_app
    pool = test_pool

    # ── Seed ──────────────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Cooldown Test Restaurant",
        bot_number_raw="+570E2ECOLDWN1",
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

    # Clear the cooldown key from any prior test run via in-process state store
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

    log.info("e2e.cooldown_start", parent_id=parent_id, bot_number=bot_number)

    # ── Create table ──────────────────────────────────────────────────────────
    create_table_resp = await client.post("/api/tables", headers=branch_headers)
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_id: str = create_table_resp.json()["table_id"]
    log.info("e2e.cooldown_table_created", table_id=table_id)

    # ── Customer A: QR scan → opens session ───────────────────────────────────
    qr_message = f"Hola, acabo de llegar a la mesa [table_id:{table_id}]"
    log.info("e2e.cooldown_customer_a", phone=PHONE_A, text=qr_message)
    t_a = time.monotonic()
    processed_a = await simulate_whatsapp_inbound(
        client, pool,
        phone=PHONE_A_RAW,
        text=qr_message,
        bot_number=bot_number,
    )
    log.info(
        "e2e.cooldown_customer_a_done",
        processed=processed_a,
        elapsed=round(time.monotonic() - t_a, 1),
    )
    assert processed_a >= 1, "Customer A message was not processed"

    replies_a = wa_capture.texts_to(PHONE_A_RAW)
    assert len(replies_a) >= 1, "Bot sent no reply to customer A"
    log.info("e2e.cooldown_customer_a_reply", reply_preview=replies_a[-1][:100])

    # ── Assert: session_A is active ───────────────────────────────────────────
    session_a = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_cooldown_assert_session_a"):
            async with pool.acquire() as conn:
                session_a = await conn.fetchrow(
                    "SELECT id, phone, table_id, status FROM table_sessions "
                    "WHERE phone = $1 AND table_id = $2 AND status = 'active' LIMIT 1",
                    PHONE_A, table_id,
                )
        if session_a:
            break
        await asyncio.sleep(0.5)

    assert session_a is not None, (
        f"Expected active session for customer A (phone={PHONE_A}, table_id={table_id}), "
        "got None. The QR scan did not open a session."
    )
    log.info("e2e.cooldown_session_a_active", session_id=str(session_a["id"]))

    # ── Customer B: same QR (no sleep — cooldown still active) ────────────────
    # Anti-permissiveness rule #4: no sleep before customer B.
    # The cooldown is set by customer A's session open; B is blocked immediately.
    log.info("e2e.cooldown_customer_b", phone=PHONE_B, text=qr_message)
    t_b = time.monotonic()
    processed_b = await simulate_whatsapp_inbound(
        client, pool,
        phone=PHONE_B_RAW,
        text=qr_message,
        bot_number=bot_number,
    )
    log.info(
        "e2e.cooldown_customer_b_done",
        processed=processed_b,
        elapsed=round(time.monotonic() - t_b, 1),
    )
    assert processed_b >= 1, "Customer B message was not processed"

    replies_b = wa_capture.texts_to(PHONE_B_RAW)
    assert len(replies_b) >= 1, "Bot sent no reply to customer B"
    last_b_reply = replies_b[-1]
    log.info("e2e.cooldown_customer_b_reply", reply_preview=last_b_reply[:150])

    # ── Assert: bot told B the table is occupied ──────────────────────────────
    last_b_lower = last_b_reply.lower()
    occupied_found = any(kw in last_b_lower for kw in _OCCUPIED_KEYWORDS)

    log.info(
        "e2e.cooldown_occupied_check",
        occupied_found=occupied_found,
        checked_keywords=_OCCUPIED_KEYWORDS,
        reply=last_b_lower[:200],
    )

    assert occupied_found, (
        f"RULE #5 BROKEN (or LLM used unexpected language): Bot reply to customer B "
        f"does not mention table being occupied. "
        f"Reply: '{last_b_reply[:300]}'\n"
        f"Expected at least one of: {_OCCUPIED_KEYWORDS}\n"
        "Check state_store.table_cooldown_acquire and the salon system prompt for "
        "'mesa ocupada' / 'table in use' messaging."
    )

    # ── Assert: only ONE active session on the table ──────────────────────────
    with bypass_tenant_scope("e2e_cooldown_assert_single_session"):
        async with pool.acquire() as conn:
            active_session_count = await conn.fetchval(
                "SELECT COUNT(*) FROM table_sessions "
                "WHERE table_id = $1 AND status = 'active'",
                table_id,
            )

    log.info(
        "e2e.cooldown_active_sessions",
        count=active_session_count,
        table_id=table_id,
    )

    assert active_session_count == 1, (
        f"RULE #5 BROKEN: Expected exactly 1 active table_session for table_id={table_id}, "
        f"got {active_session_count}. "
        "Customer B opened a parallel session despite the cooldown lock. "
        "Check table_cooldown_acquire in state_store.py and its call site in agent.py."
    )

    print(
        f"\n[OK] Rule #5 enforced: table cooldown blocks second customer.\n"
        f"  table_id={table_id}, org_id={parent_id}\n"
        f"  Customer A: session opened, cooldown acquired\n"
        f"  Customer B: blocked (1 active session only)\n"
        f"  Occupied keyword found in B's reply: {occupied_found}"
    )
