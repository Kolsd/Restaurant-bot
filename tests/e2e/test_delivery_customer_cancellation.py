"""
tests/e2e/test_delivery_customer_cancellation.py — E2E guard tests for customer-initiated order cancellation.

Two tests:

1. test_cancel_pending_order_success
   - Full 4-turn delivery flow up to 'pendiente'.
   - Customer turn: "Quiero cancelar mi pedido".
   - Assert: orders.status == 'cancelado', cancelled_at IS NOT NULL,
             bot reply mentions "cancel", orders.paid == False.

2. test_cancel_confirmed_order_rejected
   - Same 4-turn delivery flow up to 'pendiente'.
   - Admin PATCH pendiente → confirmado via /api/delivery/orders/{id}/status.
   - Customer turn: "Quiero cancelar mi pedido".
   - Assert: orders.status still == 'confirmado' (unchanged — NOT cancelado).
   - Assert: bot reply mentions contact wording ("comunícate", "teléfono", "restaurante").

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — isolated Postgres test DB
  DISABLE_EMBEDDED_WORKER=1  — set automatically by conftest.py

Run:
  pytest tests/e2e/test_delivery_customer_cancellation.py -v -m e2e --tb=short
"""
from __future__ import annotations

import asyncio
import re
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

# ── Test constants ─────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573001112233"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

CUSTOMER_PHONE_RAW_2 = "+573001112244"
CUSTOMER_PHONE_2 = _normalize_phone(CUSTOMER_PHONE_RAW_2)

CUSTOMER_ADDRESS = "Carrera 7 # 32-10, Bogota"

MENU = {
    "Empanadas": [
        {
            "name": "Empanada de Pipian",
            "description": "Empanada criolla de pipian",
            "price": 12000,
            "active": True,
        }
    ]
}

PAYMENT_METHODS = ["Nequi", "Efectivo"]


# ── App fixture ────────────────────────────────────────────────────────────────

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


# ── Helper: run 4-turn delivery flow and return order_id ──────────────────────

async def _run_delivery_flow(
    client: AsyncClient,
    pool: asyncpg.Pool,
    wa_capture: WACapture,
    *,
    customer_phone_raw: str,
    customer_phone: str,
    bot_number: str,
) -> str:
    """
    Execute the standard 4-turn delivery flow.
    Returns the order_id once the order is in status 'pendiente'.
    """
    # Turn 1: item + type
    t1 = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=customer_phone_raw,
        text="Hola, quiero pedir a domicilio una empanada de pipian",
        bot_number=bot_number,
    )
    log.info("e2e.cancel.turn_1_done", processed=processed_1, elapsed=round(time.monotonic() - t1, 1))
    assert processed_1 >= 1, "Turn 1 not processed"

    replies_1 = wa_capture.texts_to(customer_phone_raw)
    assert len(replies_1) >= 1, "Turn 1: bot sent no reply"

    # Turn 2: address
    t2 = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=customer_phone_raw,
        text=f"Mi direccion es {CUSTOMER_ADDRESS}",
        bot_number=bot_number,
    )
    log.info("e2e.cancel.turn_2_done", processed=processed_2, elapsed=round(time.monotonic() - t2, 1))
    assert processed_2 >= 1, "Turn 2 not processed"

    replies_2 = wa_capture.texts_to(customer_phone_raw)
    assert len(replies_2) >= 2, "Turn 2: bot sent no reply after address"

    # Turn 3: payment method
    t3 = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=customer_phone_raw,
        text="Efectivo",
        bot_number=bot_number,
    )
    log.info("e2e.cancel.turn_3_done", processed=processed_3, elapsed=round(time.monotonic() - t3, 1))
    assert processed_3 >= 1, "Turn 3 not processed"

    # Turn 4: confirmation
    t4 = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=customer_phone_raw,
        text="si confirmo",
        bot_number=bot_number,
    )
    log.info("e2e.cancel.turn_4_done", processed=processed_4, elapsed=round(time.monotonic() - t4, 1))
    assert processed_4 >= 1, "Turn 4 not processed"

    # Poll for order in DB
    order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_cancel_order_poll"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, status, paid
                    FROM orders
                    WHERE phone = $1
                      AND order_type = 'domicilio'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    customer_phone,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No delivery order found in DB for phone={customer_phone} after 4-turn flow. "
        "Verify ANTHROPIC_API_KEY is valid and the bot resolves to the seeded restaurant."
    )
    assert order_row["status"] == "pendiente", (
        f"Expected 'pendiente' after 4-turn flow, got '{order_row['status']}'"
    )
    return str(order_row["id"])


# ── Test 1: Happy path — cancel a 'pendiente' order ───────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cancel_pending_order_success(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Flow:
      Turns 1-4: full delivery order → status='pendiente'.
      Turn 5: "Quiero cancelar mi pedido".

    Assertions:
      - orders.status == 'cancelado'
      - orders.cancelled_at IS NOT NULL
      - orders.paid == False
      - Bot reply contains "cancel" (case-insensitive)
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Cancel Test Restaurant",
        bot_number_raw="+570E2ECANCEL01",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=0,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)

    # Reset in-process state_store fallback
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info("e2e.cancel_success.start", org_id=org_id, bot=bot_number, phone=CUSTOMER_PHONE)

    order_id = await _run_delivery_flow(
        client, pool, wa_capture,
        customer_phone_raw=CUSTOMER_PHONE_RAW,
        customer_phone=CUSTOMER_PHONE,
        bot_number=bot_number,
    )
    log.info("e2e.cancel_success.order_is_pendiente", order_id=order_id)

    # ── Turn 5: Customer cancels ───────────────────────────────────────────────
    replies_before = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))

    t5 = time.monotonic()
    processed_5 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Quiero cancelar mi pedido",
        bot_number=bot_number,
    )
    log.info("e2e.cancel_success.turn_5_done", processed=processed_5, elapsed=round(time.monotonic() - t5, 1))
    assert processed_5 >= 1, "Turn 5 (cancel) not processed"

    await asyncio.sleep(0.3)

    # ── Assert: bot reply mentions cancellation ────────────────────────────────
    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    new_replies = all_replies[replies_before:]
    log.info("e2e.cancel_success.new_replies", count=len(new_replies), replies=new_replies)

    assert len(new_replies) >= 1, (
        f"Bot sent no reply after cancel request. All replies: {all_replies}"
    )
    cancel_reply = " ".join(new_replies).lower()
    assert re.search(r"cancel", cancel_reply), (
        f"Expected bot reply to mention cancellation. Got: {cancel_reply!r}"
    )

    # ── Assert: DB state ────────────────────────────────────────────────────────
    with bypass_tenant_scope("e2e_cancel_success_assert_db"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                """
                SELECT status, paid, cancelled_at
                FROM orders
                WHERE id = $1
                """,
                order_id,
            )

    assert final_row is not None, f"Order {order_id} disappeared from DB"

    log.info(
        "e2e.cancel_success.db_state",
        order_id=order_id,
        status=final_row["status"],
        paid=final_row["paid"],
        cancelled_at=str(final_row["cancelled_at"]),
    )

    assert final_row["status"] == "cancelado", (
        f"Expected status='cancelado', got '{final_row['status']}'. "
        f"Check db_cancel_pending_order in orders_repo.py."
    )
    assert final_row["cancelled_at"] is not None, (
        f"cancelled_at should be set after cancellation, got None. "
        f"Check that migration 0050 ran (alembic upgrade head)."
    )
    assert final_row["paid"] is False, (
        f"Order paid flag should remain False after customer cancellation. "
        f"Got paid={final_row['paid']}."
    )

    log.info("e2e.cancel_success.passed", order_id=order_id)
    print(
        f"\n[OK] test_cancel_pending_order_success: order {order_id} "
        f"successfully cancelled by customer. DB status=cancelado, paid=False, "
        f"cancelled_at set. Bot reply confirmed cancellation."
    )


# ── Test 2: Adversarial — cancel after kitchen confirms (should be rejected) ──

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cancel_confirmed_order_rejected(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Flow:
      Turns 1-4: full delivery order → status='pendiente'.
      Admin PATCH: pendiente → confirmado.
      Turn 5: "Quiero cancelar mi pedido".

    Assertions:
      - orders.status == 'confirmado' (unchanged — NOT cancelado).
      - Bot reply contains direct-contact wording: "comunícate", "teléfono", or "restaurante".
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Cancel Confirmed Restaurant",
        bot_number_raw="+570E2ECANCEL02",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=0,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, org_id)

    # Reset in-process state_store fallback
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info("e2e.cancel_rejected.start", org_id=org_id, bot=bot_number, phone=CUSTOMER_PHONE_2)

    order_id = await _run_delivery_flow(
        client, pool, wa_capture,
        customer_phone_raw=CUSTOMER_PHONE_RAW_2,
        customer_phone=CUSTOMER_PHONE_2,
        bot_number=bot_number,
    )
    log.info("e2e.cancel_rejected.order_is_pendiente", order_id=order_id)

    # ── Admin PATCH: pendiente → confirmado ────────────────────────────────────
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    log.info("e2e.cancel_rejected.admin_patch_confirmado", order_id=order_id)
    patch_resp = await client.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "confirmado"},
        headers=auth_headers,
    )
    assert patch_resp.status_code == 200, (
        f"Admin PATCH to 'confirmado' failed: {patch_resp.status_code} {patch_resp.text}"
    )

    # Verify DB shows 'confirmado'
    with bypass_tenant_scope("e2e_cancel_rejected_assert_confirmado"):
        async with pool.acquire() as conn:
            mid_row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1", order_id
            )
    assert mid_row["status"] == "confirmado", (
        f"Expected 'confirmado' after admin PATCH, got '{mid_row['status']}'"
    )
    log.info("e2e.cancel_rejected.confirmed_in_db", status=mid_row["status"])

    # ── Turn 5: Customer tries to cancel ──────────────────────────────────────
    replies_before = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW_2))

    t5 = time.monotonic()
    processed_5 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_2,
        text="Quiero cancelar mi pedido",
        bot_number=bot_number,
    )
    log.info("e2e.cancel_rejected.turn_5_done", processed=processed_5, elapsed=round(time.monotonic() - t5, 1))
    assert processed_5 >= 1, "Turn 5 (cancel attempt) not processed"

    await asyncio.sleep(0.3)

    # ── Assert: DB status unchanged ────────────────────────────────────────────
    with bypass_tenant_scope("e2e_cancel_rejected_assert_final"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1", order_id
            )
    assert final_row is not None, f"Order {order_id} disappeared from DB"

    log.info(
        "e2e.cancel_rejected.db_final",
        order_id=order_id,
        status=final_row["status"],
    )

    assert final_row["status"] == "confirmado", (
        f"CRITICAL: expected status='confirmado' (unchanged) after cancel attempt on a "
        f"confirmed order, got '{final_row['status']}'. "
        f"The cancel guard in execute_external_action is not checking order status correctly."
    )

    # ── Assert: bot reply directs customer to contact restaurant ───────────────
    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW_2)
    new_replies = all_replies[replies_before:]
    log.info("e2e.cancel_rejected.new_replies", count=len(new_replies), replies=new_replies)

    assert len(new_replies) >= 1, (
        f"Bot sent no reply after cancel request on confirmed order. All replies: {all_replies}"
    )
    reject_reply = " ".join(new_replies).lower()
    contact_patterns = [r"comun[ií]cate", r"tel[eé]fono", r"restaurante", r"contacta", r"directamente"]
    contact_found = any(re.search(pat, reject_reply) for pat in contact_patterns)
    assert contact_found, (
        f"Expected bot reply to direct customer to contact restaurant directly. "
        f"Checked patterns: {contact_patterns}. "
        f"Got: {reject_reply!r}"
    )

    log.info("e2e.cancel_rejected.passed", order_id=order_id)
    print(
        f"\n[OK] test_cancel_confirmed_order_rejected: order {order_id} "
        f"remained 'confirmado' after customer cancel attempt. "
        f"Bot correctly directed customer to contact restaurant. "
        f"Guard at execute_external_action confirmed working."
    )
