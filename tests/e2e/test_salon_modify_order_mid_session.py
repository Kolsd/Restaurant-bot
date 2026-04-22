"""
tests/e2e/test_salon_modify_order_mid_session.py — E2E: add items to an
in-flight salon order.

Flow:
  1. Seed restaurant with menu including 2 dishes at different prices.
  2. QR scan opens session.
  3. Customer orders 2 empanadas → confirms.
  4. Assert base table_orders row exists with total=30000.
  5. Turn N+1: "Quiero agregar también 1 ceviche especial" → confirm.
  6. Assert a SECOND table_orders row with base_order_id pointing to original
     AND containing the ceviche.

Validates agent_salon.py sub-order path (line ~738) — "Sub-orden adicional"
logic that creates a new table_orders row chained via base_order_id.
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
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.money import to_decimal

log = get_logger(__name__)

CUSTOMER_PHONE_RAW = "+573004441111"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

MENU = {
    "Empanadas": [
        {
            "name": "Empanaditas de Carne (3 uds)",
            "description": "Empanadas fritas rellenas de carne",
            "price": 15000,
            "active": True,
        }
    ],
    "Pescados": [
        {
            "name": "Ceviche Especial",
            "description": "Ceviche mixto con camarón y pescado",
            "price": 25000,
            "active": True,
        }
    ],
}


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


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_salon_add_item_after_confirm_creates_sub_order(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Salon Modify Order Test",
        bot_number_raw="+570E2ESALONMOD",
        menu=MENU,
        payment_methods=["Nequi", "Efectivo"],
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    branch_1_id = restaurant["branches"][0]["id"]

    await truncate_e2e_data(pool, parent_id)
    await truncate_e2e_data(pool, branch_1_id)

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
    branch_headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(branch_1_id),
    }

    # Create table
    create_resp = await client.post("/api/tables", headers=branch_headers)
    assert create_resp.status_code == 200
    table_id = create_resp.json()["table_id"]

    # Turn 1 — QR scan
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=f"Hola, estoy en la mesa [table_id:{table_id}]",
        bot_number=bot_number,
    )

    # Turn 2 — initial order
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Quiero pedir 2 empanaditas de carne",
        bot_number=bot_number,
    )

    # Turn 3 — confirm initial order (unambiguous phrasing)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Sí, confirmo las 2 empanaditas de carne. Procede con el pedido.",
        bot_number=bot_number,
    )

    # ── Assert base order exists ─────────────────────────────────────────
    base_order = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_modify_assert_base"):
            async with pool.acquire() as conn:
                base_order = await conn.fetchrow(
                    """SELECT id, total, items, base_order_id, sub_number
                       FROM table_orders
                       WHERE phone=$1 AND table_id=$2
                       ORDER BY created_at ASC LIMIT 1""",
                    CUSTOMER_PHONE, table_id,
                )
        if base_order:
            break
        await asyncio.sleep(0.5)

    assert base_order is not None, (
        f"No base table_order created for phone={CUSTOMER_PHONE}. "
        "Initial order did not commit."
    )
    base_order_id = base_order["id"]
    base_total = to_decimal(base_order["total"])
    assert base_total >= Decimal("15000"), (
        f"Base order total too low: got {base_total}. "
        "LLM may have misread quantities."
    )
    log.info("e2e.modify.base_order_ok", id=base_order_id, total=str(base_total))

    # Turn 4 — add ceviche mid-session
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Quiero agregar también 1 ceviche especial, por favor.",
        bot_number=bot_number,
    )

    # Turn 5 — confirm addition (unambiguous)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Sí, confirmo el ceviche especial adicional. Procede.",
        bot_number=bot_number,
    )

    # ── Assert a NEW sub-order row was created OR base order was updated ──
    # The agent_salon.py "Sub-orden adicional" path creates a new
    # table_orders row with base_order_id = original id.
    sub_order = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_modify_assert_sub"):
            async with pool.acquire() as conn:
                sub_order = await conn.fetchrow(
                    """SELECT id, total, items, base_order_id, sub_number
                       FROM table_orders
                       WHERE phone=$1 AND table_id=$2 AND id != $3
                       ORDER BY created_at DESC LIMIT 1""",
                    CUSTOMER_PHONE, table_id, base_order_id,
                )
        if sub_order:
            break
        await asyncio.sleep(0.5)

    assert sub_order is not None, (
        f"No sub-order created after adding ceviche. "
        f"Expected a new table_orders row with base_order_id={base_order_id} "
        "for the adicional. The mid-session modify path did not fire."
    )

    # base_order_id on the new row must point back to the original
    assert str(sub_order["base_order_id"]) == str(base_order_id), (
        f"Sub-order base_order_id={sub_order['base_order_id']} does not "
        f"point back to base={base_order_id}. Chain is broken."
    )

    # sub_number must be > 1 (original is sub_number=1)
    assert sub_order["sub_number"] >= 2, (
        f"Expected sub_number>=2 on adicional, got {sub_order['sub_number']}."
    )

    # Ceviche must appear in the sub-order's items
    sub_items_raw = sub_order["items"]
    if isinstance(sub_items_raw, str):
        import json as _json
        sub_items_raw = _json.loads(sub_items_raw)
    names_lower = " ".join(
        (it.get("name") or "").lower() for it in (sub_items_raw or [])
    )
    assert "ceviche" in names_lower, (
        f"Sub-order items missing 'ceviche'. items={sub_items_raw}"
    )

    log.info(
        "e2e.modify.sub_order_ok",
        id=sub_order["id"],
        base=base_order_id,
        sub_num=sub_order["sub_number"],
        total=str(to_decimal(sub_order["total"])),
    )

    print(
        f"\n[OK] salon modify mid-session: base={base_order_id} + "
        f"sub={sub_order['id']} (sub_number={sub_order['sub_number']}) "
        "with ceviche."
    )
