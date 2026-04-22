"""
tests/e2e/test_delivery_multi_sku.py — E2E test: multi-SKU delivery order.

Menu with 3 dishes at different prices:
  - Empanaditas de Carne:  15000
  - Hamburguesa Clásica:   28000
  - Agua Embotellada:       5000

Customer order: 2 empanadas + 1 hamburguesa + 2 aguas
  = 2×15000 + 1×28000 + 2×5000 = 30000 + 28000 + 10000 = 68000

Assertions (all HARD — no >= , no approximate):
  1. orders.total == Decimal("68000") EXACTLY
  2. orders.items has 3 distinct dishes, quantities [2, 1, 2] matched by name
  3. After commit, each inventory row is decremented by its quantity:
       empanadas: 10-2=8, hamburguesa: 10-1=9, aguas: 10-2=8

If the LLM miscounts quantities or the total is wrong, this test FAILS — that is a
real regression in multi-item parsing.

Run:
  pytest tests/e2e/test_delivery_multi_sku.py -v -m e2e
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.money import to_decimal

log = get_logger(__name__)

CUSTOMER_PHONE_RAW = "+573022334455"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)
CUSTOMER_ADDRESS = "Avenida El Dorado # 68C-61, Bogotá"
CUSTOMER_LAT = 4.711200
CUSTOMER_LON = -74.072300

# Expected order composition
DISH_EMPANADAS = "Empanaditas de Carne"
DISH_HAMBURGUESA = "Hamburguesa Clásica"
DISH_AGUA = "Agua Embotellada"

PRICE_EMPANADAS = Decimal("15000")
PRICE_HAMBURGUESA = Decimal("28000")
PRICE_AGUA = Decimal("5000")

QTY_EMPANADAS = 2
QTY_HAMBURGUESA = 1
QTY_AGUA = 2

EXPECTED_TOTAL = Decimal("68000")  # 2*15000 + 1*28000 + 2*5000

STOCK_INITIAL = 10  # each SKU starts with stock=10

MENU = {
    "Principal": [
        {
            "name": DISH_EMPANADAS,
            "description": "Empanadas fritas rellenas de carne y papa",
            "price": int(PRICE_EMPANADAS),
            "active": True,
        },
        {
            "name": DISH_HAMBURGUESA,
            "description": "Hamburguesa con doble carne, queso y papas fritas",
            "price": int(PRICE_HAMBURGUESA),
            "active": True,
        },
        {
            "name": DISH_AGUA,
            "description": "Agua embotellada 500ml",
            "price": int(PRICE_AGUA),
            "active": True,
        },
    ]
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
async def test_delivery_multi_sku_exact_total_and_stock(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Multi-SKU delivery: 2 empanadas + 1 hamburguesa + 2 aguas = 68000.

    HARD assertions:
      - Total == 68000 exactly (no tolerance).
      - Items list has all 3 distinct dishes in correct quantities.
      - Post-commit inventory stock decremented by the ordered quantities.

    If the LLM miscounts, the total will differ from 68000 and this test fails.
    That is intended — it is a regression in multi-item parsing.
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Multi-SKU Restaurant",
        bot_number_raw="+570E2EMULTISKU",
        menu=MENU,
        payment_methods=["Efectivo"],
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),
            (4.609710, -74.081741),
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, parent_id)
    for b in restaurant["branches"]:
        await truncate_e2e_data(pool, b["id"])

    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    # ── Seed inventory: 3 SKUs linked to the 3 dish names ─────────────────────
    # Uses the legacy linked_dishes path (no dish_recipes rows) — same as stock test.
    inv_ids: dict[str, int] = {}
    with bypass_tenant_scope("e2e_multisku_seed_inventory"):
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM inventory WHERE org_id = $1", parent_id)
            for dish_name in [DISH_EMPANADAS, DISH_HAMBURGUESA, DISH_AGUA]:
                row = await conn.fetchrow(
                    """
                    INSERT INTO inventory
                      (org_id, name, unit, current_stock, min_stock, linked_dishes, cost_per_unit)
                    VALUES ($1, $2, 'und', $3, 0, $4::jsonb, 0)
                    RETURNING id, current_stock
                    """,
                    parent_id,
                    dish_name,
                    Decimal(STOCK_INITIAL),
                    json.dumps([dish_name]),
                )
                inv_ids[dish_name] = row["id"]
                assert to_decimal(row["current_stock"]) == Decimal(STOCK_INITIAL), (
                    f"Inventory seed failed for {dish_name}: got {row['current_stock']}"
                )

    log.info(
        "e2e.multisku.start",
        parent_id=parent_id,
        bot_number=bot_number,
        inv_ids=inv_ids,
        expected_total=str(EXPECTED_TOTAL),
    )

    # ── Turn 1: Delivery intent with all 3 items in one message ───────────────
    # Phrasing specifies quantities explicitly so the LLM doesn't guess.
    turn1_text = (
        "Hola, quiero hacer un pedido a domicilio: "
        "2 empanaditas de carne, 1 hamburguesa clásica y 2 aguas embotelladas"
    )
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=turn1_text,
        bot_number=bot_number,
    )

    # ── Turn 2: Address ────────────────────────────────────────────────────────
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=f"Mi direccion es {CUSTOMER_ADDRESS}",
        bot_number=bot_number,
    )

    # GPS location to trigger branch routing
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="",
        bot_number=bot_number,
        lat=CUSTOMER_LAT,
        lon=CUSTOMER_LON,
    )

    # ── Turn 3: Payment method ─────────────────────────────────────────────────
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Efectivo",
        bot_number=bot_number,
    )

    # ── Turn 4: Confirmation ───────────────────────────────────────────────────
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="si confirmo",
        bot_number=bot_number,
    )
    assert processed_4 >= 1, "Confirmation turn not processed"

    # ── Poll for order in DB ───────────────────────────────────────────────────
    order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_multisku_poll_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, org_id, total, items, order_type, status
                    FROM orders
                    WHERE phone = $1
                      AND org_id = $2
                      AND order_type = 'domicilio'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    parent_id,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No delivery order found for phone={CUSTOMER_PHONE}, org={parent_id}. "
        "Check ANTHROPIC_API_KEY and bot_number resolution."
    )

    order_id = order_row["id"]
    order_total = to_decimal(order_row["total"])

    # Parse items (asyncpg returns jsonb as dict/list already; may also be str)
    raw_items = order_row["items"]
    if isinstance(raw_items, str):
        import json as _json
        raw_items = _json.loads(raw_items)
    items: list[dict] = raw_items if isinstance(raw_items, list) else []

    log.info(
        "e2e.multisku.order_found",
        order_id=order_id,
        total=str(order_total),
        item_count=len(items),
        items_summary=[
            {"name": i.get("name"), "qty": i.get("qty", i.get("quantity"))}
            for i in items
        ],
    )

    # ── HARD ASSERTION 1: exact total ─────────────────────────────────────────
    assert order_total == EXPECTED_TOTAL, (
        f"REGRESSION — multi-SKU total is {order_total}, expected EXACTLY {EXPECTED_TOTAL}. "
        f"Order items: {items}. "
        "The LLM miscounted quantities or subtotals. Check multi-item parsing in agent_external.py."
    )

    # ── HARD ASSERTION 2: all 3 dishes present in correct quantities ──────────
    def _qty(item: dict) -> int:
        return int(item.get("qty", item.get("quantity", 0)))

    # Build a name→qty map; use case-insensitive partial match on the 3 expected names
    def _find_item(items: list[dict], name_fragment: str) -> dict | None:
        for it in items:
            if name_fragment.lower() in it.get("name", "").lower():
                return it
        return None

    item_empanadas = _find_item(items, "empanadita")
    item_hamburguesa = _find_item(items, "hamburguesa")
    item_agua = _find_item(items, "agua")

    assert item_empanadas is not None, (
        f"'{DISH_EMPANADAS}' not found in order items. items={[i.get('name') for i in items]}"
    )
    assert item_hamburguesa is not None, (
        f"'{DISH_HAMBURGUESA}' not found in order items. items={[i.get('name') for i in items]}"
    )
    assert item_agua is not None, (
        f"'{DISH_AGUA}' not found in order items. items={[i.get('name') for i in items]}"
    )

    assert _qty(item_empanadas) == QTY_EMPANADAS, (
        f"Expected {QTY_EMPANADAS}x empanaditas, got {_qty(item_empanadas)}. "
        f"LLM quantity parsing regression: {item_empanadas}"
    )
    assert _qty(item_hamburguesa) == QTY_HAMBURGUESA, (
        f"Expected {QTY_HAMBURGUESA}x hamburguesa, got {_qty(item_hamburguesa)}. "
        f"LLM quantity parsing regression: {item_hamburguesa}"
    )
    assert _qty(item_agua) == QTY_AGUA, (
        f"Expected {QTY_AGUA}x aguas, got {_qty(item_agua)}. "
        f"LLM quantity parsing regression: {item_agua}"
    )

    # ── HARD ASSERTION 3: inventory decremented by ordered quantities ──────────
    # After commit_order_transaction, each inventory row must be decremented exactly.
    with bypass_tenant_scope("e2e_multisku_check_inventory"):
        async with pool.acquire() as conn:
            stock_rows = await conn.fetch(
                "SELECT id, current_stock FROM inventory WHERE id = ANY($1::int[])",
                list(inv_ids.values()),
            )
    stock_map = {r["id"]: to_decimal(r["current_stock"]) for r in stock_rows}

    expected_empanadas_stock = Decimal(STOCK_INITIAL - QTY_EMPANADAS)  # 10-2 = 8
    expected_hamburguesa_stock = Decimal(STOCK_INITIAL - QTY_HAMBURGUESA)  # 10-1 = 9
    expected_agua_stock = Decimal(STOCK_INITIAL - QTY_AGUA)  # 10-2 = 8

    actual_empanadas = stock_map.get(inv_ids[DISH_EMPANADAS])
    actual_hamburguesa = stock_map.get(inv_ids[DISH_HAMBURGUESA])
    actual_agua = stock_map.get(inv_ids[DISH_AGUA])

    log.info(
        "e2e.multisku.inventory_post_commit",
        empanadas=str(actual_empanadas),
        hamburguesa=str(actual_hamburguesa),
        agua=str(actual_agua),
    )

    assert actual_empanadas == expected_empanadas_stock, (
        f"Empanaditas stock: expected {expected_empanadas_stock}, got {actual_empanadas}. "
        "Inventory deduction wrong or inventory row not found."
    )
    assert actual_hamburguesa == expected_hamburguesa_stock, (
        f"Hamburguesa stock: expected {expected_hamburguesa_stock}, got {actual_hamburguesa}. "
        "Inventory deduction wrong or inventory row not found."
    )
    assert actual_agua == expected_agua_stock, (
        f"Agua stock: expected {expected_agua_stock}, got {actual_agua}. "
        "Inventory deduction wrong or inventory row not found."
    )

    print(
        f"\n[OK] Multi-SKU delivery test passed: order {order_id}, "
        f"total={order_total} COP (EXACT), "
        f"items: {QTY_EMPANADAS}x empanaditas + {QTY_HAMBURGUESA}x hamburguesa + {QTY_AGUA}x aguas. "
        f"Inventory: empanadas={actual_empanadas}, hamburguesa={actual_hamburguesa}, agua={actual_agua}."
    )
