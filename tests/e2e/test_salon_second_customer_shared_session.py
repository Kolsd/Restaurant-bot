"""
tests/e2e/test_salon_second_customer_shared_session.py — E2E test: second customer
on an occupied table is blocked.

SCOPE NOTE: The current codebase does NOT support "joining" an existing table session
(merging B's order into A's session). table_cooldown_acquire (Rule #5) prevents B
from opening ANY session on a table that already has an active session. This test
validates that protection explicitly.

This test covers the same code path as test_salon_table_cooldown.py but asserts
the DB state more precisely:
  - table_sessions COUNT == 1 for the table (not 0 or 2)
  - phone B has NO active table_session row at all
  - phone B's reply contains an "occupied" keyword

If the table-join feature is added in future (B merging into A's session), this test
should be updated to validate the join flow. Until then, blocked = correct behaviour.

Run:
  pytest tests/e2e/test_salon_second_customer_shared_session.py -v -m e2e --tb=short
"""
from __future__ import annotations

import asyncio

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

# ── Constants ──────────────────────────────────────────────────────────────────

PHONE_A_RAW = "+573009991001"
PHONE_A = _normalize_phone(PHONE_A_RAW)

PHONE_B_RAW = "+573009991002"
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

# Keywords the bot should use when telling customer B the table is occupied.
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
async def test_second_customer_on_occupied_table_is_blocked(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Customer A opens a salon session. Customer B scans the SAME table immediately.

    Because table_cooldown_acquire (Rule #5) is still held by A's session,
    B is blocked and receives an "occupied" message.

    DB assertions:
      - table_sessions WHERE table_id=X AND status='active' → COUNT == 1
      - table_sessions WHERE phone=PHONE_B AND table_id=X → COUNT == 0
      - bot reply to B contains an occupied keyword
    """
    client = e2e_app
    pool = test_pool

    # ── Seed ──────────────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Shared Session Restaurant",
        bot_number_raw="+570E2ESESSION",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    branch_id = restaurant["branches"][0]["id"]

    await truncate_e2e_data(pool, org_id)

    # Clear in-process cooldown state from prior tests
    try:
        import app.services.state_store as ss
        for attr in ["_fb_cooldown", "_fb_nps", "_fb_checkout",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    # ── Create a table via admin API ──────────────────────────────────────────
    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}", "X-Branch-ID": str(branch_id)}

    create_resp = await client.post("/api/tables", headers=auth_headers)
    assert create_resp.status_code == 200, (
        f"POST /api/tables failed: {create_resp.status_code} {create_resp.text}"
    )
    table_id: str = create_resp.json()["table_id"]
    log.info("e2e.shared_session.table_created", table_id=table_id)

    # ── Customer A: scan table → open session ─────────────────────────────────
    qr_message = f"Hola, acabo de llegar a la mesa [table_id:{table_id}]"
    processed_a = await simulate_whatsapp_inbound(
        client, pool,
        phone=PHONE_A_RAW,
        text=qr_message,
        bot_number=bot_number,
    )
    assert processed_a >= 1, "Customer A message was not processed"

    replies_a = wa_capture.texts_to(PHONE_A_RAW)
    assert len(replies_a) >= 1, "Bot sent no reply to customer A"
    log.info("e2e.shared_session.customer_a_reply", reply=replies_a[-1][:100])

    # ── Wait briefly for session to be committed ──────────────────────────────
    session_a = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_shared_session_assert_a"):
            async with pool.acquire() as conn:
                session_a = await conn.fetchrow(
                    "SELECT id, phone FROM table_sessions "
                    "WHERE phone = $1 AND table_id = $2 AND status = 'active' LIMIT 1",
                    PHONE_A, table_id,
                )
        if session_a:
            break
        await asyncio.sleep(0.5)

    assert session_a is not None, (
        f"Customer A's session was not created (phone={PHONE_A}, table_id={table_id}). "
        "QR scan did not open a salon session."
    )
    log.info("e2e.shared_session.session_a_active", session_id=str(session_a["id"]))

    # ── Customer B: same table, NO sleep — cooldown is active ────────────────
    processed_b = await simulate_whatsapp_inbound(
        client, pool,
        phone=PHONE_B_RAW,
        text=qr_message,
        bot_number=bot_number,
    )
    assert processed_b >= 1, "Customer B message was not processed"

    replies_b = wa_capture.texts_to(PHONE_B_RAW)
    assert len(replies_b) >= 1, "Bot sent no reply to customer B"
    last_b = replies_b[-1]
    log.info("e2e.shared_session.customer_b_reply", reply=last_b[:150])

    # ── ASSERTION 1: Bot told B the table is occupied ─────────────────────────
    b_lower = last_b.lower()
    occupied = any(kw in b_lower for kw in _OCCUPIED_KEYWORDS)
    assert occupied, (
        f"Rule #5 BROKEN (or unexpected LLM language): Bot did not tell B the table is occupied. "
        f"Reply: '{last_b[:300]}'\n"
        f"Expected one of: {_OCCUPIED_KEYWORDS}"
    )

    # ── ASSERTION 2: Only ONE active session on the table ────────────────────
    with bypass_tenant_scope("e2e_shared_session_assert_count"):
        async with pool.acquire() as conn:
            active_count = await conn.fetchval(
                "SELECT COUNT(*) FROM table_sessions "
                "WHERE table_id = $1 AND status = 'active'",
                table_id,
            )

    assert active_count == 1, (
        f"Rule #5 BROKEN: Expected 1 active session on table_id={table_id}, got {active_count}. "
        "Customer B opened a parallel session despite the cooldown lock."
    )
    log.info("e2e.shared_session.active_session_count_ok", count=active_count)

    # ── ASSERTION 3: Customer B has NO active session ─────────────────────────
    with bypass_tenant_scope("e2e_shared_session_assert_b"):
        async with pool.acquire() as conn:
            b_session_count = await conn.fetchval(
                "SELECT COUNT(*) FROM table_sessions "
                "WHERE phone = $1 AND table_id = $2 AND status = 'active'",
                PHONE_B, table_id,
            )

    assert b_session_count == 0, (
        f"Rule #5 BROKEN: Customer B has {b_session_count} active session(s) on table_id={table_id}. "
        "A session should NOT have been created for the second customer."
    )
    log.info("e2e.shared_session.b_no_session_ok")

    print(
        f"\n[OK] Second customer on occupied table is correctly blocked.\n"
        f"  table_id={table_id}, org_id={org_id}\n"
        f"  Customer A: session opened (id={session_a['id']})\n"
        f"  Customer B: blocked — 0 sessions, 'occupied' keyword in reply"
    )
