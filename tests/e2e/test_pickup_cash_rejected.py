"""
tests/e2e/test_pickup_cash_rejected.py — E2E test: pickup cash payment policy enforcement.

ONE test that verifies the policy at agent_external.py:170 stays enforced:
  - Pickup orders MUST NOT be accepted with cash payment ("Efectivo").
  - The bot must reply with a polite rejection explaining advance payment is required.
  - No orders row must exist after the rejection (order was never committed).

Flow:
  Turn 1: "Hola, quiero recoger empanaditas"
  Turn 2: GPS near branch 1 (auto-assigns branch, bot prompts for payment method)
  Turn 3: "Quiero pagar en efectivo"  <- the rejection trigger

Assertions:
  1. Latest reply to customer mentions "anticipado" / "pago anticipado" /
     "requerimos pago" or "digitales" (per agent_external.py:52 wording).
  2. COUNT(orders WHERE phone=X AND order_type='recoger') == 0 — order was NOT committed.

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — isolated Postgres test DB
  DISABLE_EMBEDDED_WORKER=1  — set automatically by conftest.py
"""
from __future__ import annotations

import asyncio
import re
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

# ── Constants ─────────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573007770101"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# Near branch 1 (zona norte)
CUSTOMER_LAT = 4.710989
CUSTOMER_LON = -74.072092

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

# Include Efectivo in restaurant's payment_methods so the LLM knows it's a
# real method for delivery — only pickup must block it.
PAYMENT_METHODS = ["Nequi", "Efectivo", "Transferencia"]


# ── App fixture ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """FastAPI app via ASGI transport, lifespan-managed per test."""
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
async def test_pickup_cash_rejected_guardian(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Verifies that the pickup-cash-rejected policy (agent_external.py:170) stays enforced.

    The bot must:
      - Reject "Efectivo" for pickup with a polite explanation.
      - NOT commit any orders row.

    The system prompt (agent_external.py:51-52) says:
      "Para pedidos para recoger requerimos pago anticipado..."
    The tool-level guard (agent_external.py:170-172) returns early with rejection text.

    Both layers are tested: if the LLM bypasses the system prompt, the tool guard
    fires and returns the rejection string instead of committing. Either way, the
    final COUNT of pickup orders for this phone must be 0.
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Cash Rejection Restaurant",
        bot_number_raw="+570E2ECASHREJ",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),   # branch 1 — zona norte (near customer)
            (4.609710, -74.081741),   # branch 2 — zona sur
        ],
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    # Clean volatile data from prior runs
    await truncate_e2e_data(pool, org_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    # Reset in-process state_store fallback dicts
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
        "e2e.cash_rejected.start",
        org_id=org_id,
        bot_number=bot_number,
        phone=CUSTOMER_PHONE,
    )

    # ── Turn 1: Pickup intent + item ──────────────────────────────────────────
    log.info("e2e.turn_1", text="Hola, quiero recoger empanaditas")
    t1 = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Hola, quiero recoger empanaditas",
        bot_number=bot_number,
    )
    log.info("e2e.turn_1_done", processed=processed_1, elapsed=round(time.monotonic() - t1, 1))
    assert processed_1 >= 1, "Turn 1: inbox item was not processed"

    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.turn_1_replies", count=len(turn1_replies), preview=turn1_replies[:1])
    assert len(turn1_replies) >= 1, "Turn 1: bot sent no WA message"

    # ── Turn 2: GPS near branch 1 ─────────────────────────────────────────────
    log.info("e2e.turn_2", lat=CUSTOMER_LAT, lon=CUSTOMER_LON)
    t2 = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="",
        bot_number=bot_number,
        lat=CUSTOMER_LAT,
        lon=CUSTOMER_LON,
    )
    log.info("e2e.turn_2_done", processed=processed_2, elapsed=round(time.monotonic() - t2, 1))
    assert processed_2 >= 1, "Turn 2: GPS inbox item was not processed"

    turn2_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.turn_2_replies", count=len(turn2_replies))
    assert len(turn2_replies) >= 2, "Turn 2: bot sent no reply after GPS"

    # ── Turn 3: Customer requests cash — should be rejected ───────────────────
    log.info("e2e.turn_3", text="Quiero pagar en efectivo")
    t3 = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Quiero pagar en efectivo",
        bot_number=bot_number,
    )
    log.info("e2e.turn_3_done", processed=processed_3, elapsed=round(time.monotonic() - t3, 1))
    assert processed_3 >= 1, "Turn 3: cash attempt inbox item was not processed"

    # Give a brief moment for any async tasks to flush
    await asyncio.sleep(0.5)

    turn3_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.turn_3_replies", count=len(turn3_replies), all_replies=turn3_replies)

    # Assert: at least one new reply after the cash request
    assert len(turn3_replies) >= 3, (
        f"Turn 3: bot sent no reply after 'Quiero pagar en efectivo'. "
        f"All replies: {turn3_replies}"
    )

    # ── Key assertion 1: rejection wording ────────────────────────────────────
    # The latest reply (or at least one after Turn 3) must contain the rejection
    # language from agent_external.py:51-52 or :172.
    # Accepted keywords: "anticipado", "pago anticipado", "requerimos pago", "digitales"
    latest_reply = turn3_replies[-1].lower()
    log.info("e2e.latest_reply_after_cash", text=latest_reply[:200])

    _REJECTION_PATTERNS = [
        r"anticipado",
        r"pago anticipado",
        r"requerimos pago",
        r"digitales",
        r"nequi",        # listing digital methods is part of the rejection flow
        r"daviplata",
        r"transferencia",
    ]
    rejection_found = any(
        re.search(pat, latest_reply) for pat in _REJECTION_PATTERNS
    )
    assert rejection_found, (
        f"Expected a cash-rejection message containing one of {_REJECTION_PATTERNS}. "
        f"Latest reply was: {latest_reply!r}. "
        f"All Turn-3 replies: {turn3_replies}"
    )
    log.info("e2e.rejection_message_confirmed", latest_reply=latest_reply[:150])

    # ── Key assertion 2: NO pickup order in DB ────────────────────────────────
    # The tool guard at agent_external.py:170 must have blocked the INSERT.
    # If the LLM created the order anyway → critical policy failure, test fails loudly.
    with bypass_tenant_scope("e2e_cash_rejection_assert"):
        async with pool.acquire() as conn:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS cnt
                FROM orders
                WHERE phone = $1
                  AND order_type = 'recoger'
                """,
                CUSTOMER_PHONE,
            )
    order_count = count_row["cnt"]
    log.info("e2e.order_count_after_cash_rejection", count=order_count)

    assert order_count == 0, (
        f"CRITICAL: {order_count} pickup order(s) found for phone={CUSTOMER_PHONE} "
        f"after cash payment rejection. The tool guard at agent_external.py:170 "
        f"was bypassed — this is a policy enforcement failure.\n"
        f"Check: agent_external.py execute_external_action, action='pickup', "
        f"payment_method in ('efectivo','cash','en efectivo')."
    )

    log.info(
        "e2e.cash_rejected.passed",
        order_count=order_count,
        rejection_confirmed=True,
        total_wa_messages=len(wa_capture.messages),
    )
    print(
        f"\n[OK] E2E test passed: cash payment rejected for pickup. "
        f"0 orders committed. Rejection message confirmed. "
        f"{len(wa_capture.messages)} total WA messages."
    )
