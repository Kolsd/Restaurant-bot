"""
tests/e2e/test_mesero_table_session_renders.py — E2E test: mesero POS table flow.

Goal: prove the mesero POS page shows tables with real data and the action
buttons (open table → add order → mark ready → mark delivered → pay) all
connect to the backend correctly.

Steps:
  1. Seed org + location + restaurant_table + admin user
  2. Admin login → GET /api/pos/tables-status → assert table returned in available state
  3. Seed a table_order with items directly in DB (representing a bot-initiated order)
  4. GET /api/table-orders → assert our order is returned with items and computed total
  5. POST /api/table-orders/{id}/status {"status": "en_preparacion"} → assert 2xx + DB
  6. POST /api/table-orders/{id}/status {"status": "listo"} → assert DB
  7. POST /api/table-orders/{id}/status {"status": "entregado"} → assert DB
  8. POST /api/table-orders/{id}/checks/single/pay → assert check created with correct total
  9. GET /api/table-orders with table_id filter → assert DB reflects paid state

No ANTHROPIC_API_KEY required — pure operational dashboard test.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg
import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    create_admin_token,
    seed_restaurant,
    truncate_e2e_data,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.money import to_decimal

log = get_logger(__name__)

CUSTOMER_PHONE = "573002223333"
ITEM_PRICE = Decimal("28000")
ITEM_QTY = 2
EXPECTED_TOTAL = ITEM_PRICE * ITEM_QTY  # 56000


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=60.0,
        ) as client:
            yield client


# ── The test ──────────────────────────────────────────────────────────────────

@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_mesero_table_session_renders(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Waiter POS full flow:

    Setup → tables-status shows available table → seed table_order → list orders →
    status transitions (recibido→en_preparacion→listo→entregado) → pay single check →
    assert paid=true and check total matches seeded items.
    """
    pool = test_pool

    # ── Seed restaurant (single location) ───────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Mesero Test Restaurant",
        bot_number_raw="+570E2EMESERO",
        num_branches=0,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, org_id)

    # ── Resolve location_id for this org ─────────────────────────────────────────
    with bypass_tenant_scope("e2e_mesero_resolve_location"):
        async with pool.acquire() as conn:
            loc_row = await conn.fetchrow(
                "SELECT id FROM locations WHERE org_id = $1 ORDER BY id ASC LIMIT 1",
                org_id,
            )
    assert loc_row is not None, f"No location found for org_id={org_id}"
    location_id = loc_row["id"]

    # ── Seed a restaurant table ──────────────────────────────────────────────────
    table_id = str(uuid.uuid4())
    with bypass_tenant_scope("e2e_mesero_seed_table"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO restaurant_tables
                     (id, org_id, branch_id, location_id, number, name, capacity, active)
                   VALUES ($1::uuid, $2, $3::integer, $3::bigint, 99, 'Mesa E2E-01', 4, true)
                   ON CONFLICT (id) DO NOTHING""",
                table_id, org_id, location_id,
            )

    log.info(
        "e2e.mesero.setup_done",
        org_id=org_id,
        location_id=location_id,
        table_id=table_id,
        bot_number=bot_number,
    )

    # ── Admin auth ───────────────────────────────────────────────────────────────
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Branch-ID": str(location_id),
    }

    # ── Step 2: GET /api/pos/tables-status → assert table in available state ────
    tables_resp = await e2e_app.get("/api/pos/tables-status", headers=auth_headers)
    assert tables_resp.status_code == 200, (
        f"GET /api/pos/tables-status returned {tables_resp.status_code}: {tables_resp.text}. "
        "This is the data feed for the mesero page. Without it, the page is blank."
    )
    tables_data = tables_resp.json()
    all_tables = tables_data.get("tables", [])

    # LOAD-BEARING ASSERTION: our seeded table must appear
    our_table = next(
        (t for t in all_tables if str(t.get("id")) == str(table_id)), None
    )
    assert our_table is not None, (
        f"Seeded table {table_id} NOT found in /api/pos/tables-status. "
        f"Got {len(all_tables)} tables. "
        "The mesero page would not render this table — check RLS and location_id filter."
    )
    # The table must be in an available state (no active session, no pending orders)
    assert not our_table.get("session_active", False), (
        f"Table {table_id} already has an active session — test isolation issue."
    )
    log.info(
        "e2e.mesero.table_status_ok",
        table_id=table_id,
        session_active=our_table.get("session_active"),
        pending_orders=our_table.get("pending_orders"),
    )

    # ── Step 3: Seed a table_order representing a bot-placed order ───────────────
    # In production, this comes from the bot scanning a QR and placing an order.
    # Here we seed it directly so the test is independent of ANTHROPIC_API_KEY.
    order_id = str(uuid.uuid4())
    items_json = json.dumps([
        {"name": "Bandeja Paisa", "qty": int(ITEM_QTY), "price": float(ITEM_PRICE)},
    ])
    with bypass_tenant_scope("e2e_mesero_seed_order"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO table_orders
                     (id, org_id, location_id, table_id, table_name, phone, status,
                      items, total, bot_number, channel)
                   VALUES ($1::uuid, $2, $3, $4::uuid, 'Mesa E2E-01', $5, 'recibido',
                           $6::jsonb, $7, $8, 'whatsapp')""",
                order_id, org_id, location_id, table_id,
                CUSTOMER_PHONE, items_json,
                float(EXPECTED_TOTAL),
                bot_number,
            )

    log.info("e2e.mesero.order_seeded", order_id=order_id, total=float(EXPECTED_TOTAL))

    # ── Step 4: GET /api/table-orders → assert our order is returned ─────────────
    orders_resp = await e2e_app.get(
        f"/api/table-orders?table_id={table_id}",
        headers=auth_headers,
    )
    assert orders_resp.status_code == 200, (
        f"GET /api/table-orders returned {orders_resp.status_code}: {orders_resp.text}"
    )
    orders_data = orders_resp.json()
    all_orders = orders_data.get("orders", [])

    our_order = next(
        (o for o in all_orders if str(o.get("id")) == str(order_id)), None
    )
    assert our_order is not None, (
        f"Seeded table_order {order_id} NOT found in /api/table-orders. "
        f"Got {len(all_orders)} orders. "
        "The mesero page would not render this order."
    )

    # LOAD-BEARING: verify the items and total came through correctly
    returned_total = to_decimal(our_order.get("total", 0))
    assert returned_total == EXPECTED_TOTAL, (
        f"table_order total mismatch: expected {EXPECTED_TOTAL}, got {returned_total}. "
        "The mesero page would show a wrong price."
    )
    returned_items = our_order.get("items", [])
    assert len(returned_items) >= 1, (
        f"table_order has no items in API response: {our_order!r}. "
        "The mesero comanda would be blank."
    )
    log.info(
        "e2e.mesero.order_in_api",
        order_id=order_id,
        total=float(returned_total),
        items_count=len(returned_items),
    )

    # ── Step 5: POST status 'en_preparacion' ────────────────────────────────────
    prep_resp = await e2e_app.post(
        f"/api/table-orders/{order_id}/status",
        json={"status": "en_preparacion"},
        headers=auth_headers,
    )
    assert prep_resp.status_code == 200, (
        f"POST status 'en_preparacion' failed: {prep_resp.status_code} {prep_resp.text}"
    )
    with bypass_tenant_scope("e2e_mesero_assert_en_preparacion"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM table_orders WHERE id = $1", order_id,
            )
    assert row["status"] == "en_preparacion", (
        f"DB status after 'en_preparacion': expected 'en_preparacion', got '{row['status']}'"
    )

    # ── Step 6: POST status 'listo' ─────────────────────────────────────────────
    listo_resp = await e2e_app.post(
        f"/api/table-orders/{order_id}/status",
        json={"status": "listo"},
        headers=auth_headers,
    )
    assert listo_resp.status_code == 200, (
        f"POST status 'listo' failed: {listo_resp.status_code} {listo_resp.text}"
    )
    with bypass_tenant_scope("e2e_mesero_assert_listo"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM table_orders WHERE id = $1", order_id,
            )
    assert row["status"] == "listo", (
        f"DB status after 'listo': expected 'listo', got '{row['status']}'"
    )

    # ── Step 7: POST status 'entregado' ─────────────────────────────────────────
    await asyncio.sleep(0.1)  # allow rate-limit window
    entregado_resp = await e2e_app.post(
        f"/api/table-orders/{order_id}/status",
        json={"status": "entregado"},
        headers=auth_headers,
    )
    assert entregado_resp.status_code == 200, (
        f"POST status 'entregado' failed: {entregado_resp.status_code} {entregado_resp.text}"
    )
    with bypass_tenant_scope("e2e_mesero_assert_entregado"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM table_orders WHERE id = $1", order_id,
            )
    assert row["status"] == "entregado", (
        f"DB status after 'entregado': expected 'entregado', got '{row['status']}'"
    )
    log.info("e2e.mesero.order_delivered")

    # ── Step 8: POST /api/table-orders/{id}/checks/single/pay ───────────────────
    # This is the "Cobrar mesa completa" button on the caja/mesero page.
    pay_resp = await e2e_app.post(
        f"/api/table-orders/{order_id}/checks/single/pay",
        json={
            "payments": [{"method": "Efectivo", "amount": float(EXPECTED_TOTAL)}],
            "tip_amount": 0,
        },
        headers=auth_headers,
    )
    assert pay_resp.status_code == 200, (
        f"POST /checks/single/pay failed: {pay_resp.status_code} {pay_resp.text}. "
        "This is the 'Cobrar' button on the mesero page."
    )
    pay_data = pay_resp.json()
    log.info("e2e.mesero.pay_response", response=pay_data)

    # ── Step 9: Verify DB has a paid check with the correct total ────────────────
    with bypass_tenant_scope("e2e_mesero_assert_check_paid"):
        async with pool.acquire() as conn:
            check_rows = await conn.fetch(
                """SELECT tc.id, tc.status, tc.total, tc.tip_amount
                   FROM table_checks tc
                   WHERE tc.base_order_id = $1""",
                order_id,
            )
    assert len(check_rows) >= 1, (
        f"No table_checks rows found for order {order_id} after pay. "
        "The pay endpoint must create a check before marking it paid."
    )
    # Status after successful payment is 'invoiced' (when DIAN fiscal invoice is issued
    # or the local invoice fallback runs). In some configurations it may be 'paid'.
    # Either status proves the payment was processed successfully.
    PAID_STATUSES = ("paid", "invoiced")
    paid_check = next(
        (dict(r) for r in check_rows if r["status"] in PAID_STATUSES), None
    )
    assert paid_check is not None, (
        f"No check with status in {PAID_STATUSES} found. "
        f"Checks: {[dict(r) for r in check_rows]}. "
        "The pay endpoint returned 200 but the check was not marked as paid/invoiced in DB."
    )
    # LOAD-BEARING: check total must equal the order total
    check_total = to_decimal(paid_check["total"])
    assert check_total == EXPECTED_TOTAL, (
        f"Check total {check_total} != expected {EXPECTED_TOTAL}. "
        "The check total must match the items total — math error in pay_check_single."
    )
    log.info(
        "e2e.mesero.test_passed",
        order_id=order_id,
        check_id=str(paid_check["id"]),
        check_total=float(check_total),
    )
    print(
        f"\n[OK] Mesero E2E test passed: order {order_id} completed full table session "
        f"(recibido -> en_preparacion -> listo -> entregado -> cobrado). "
        f"Check total: {check_total} COP."
    )
