"""
tests/e2e/test_domiciliario_page_renders_buttons.py — E2E test: domiciliario operational page.

This test caught the "flat page without buttons" regression described by the user.

Goal:
  Prove the /domiciliario admin page renders required action buttons WHEN real data
  is seeded.  The "render proof" is: the API returns an order in a status that the
  JS uses to render "Salir a entregar", "Llegué" and "Entregado" buttons.

Steps:
  a. Seed org + location + delivery order in status 'confirmado'
  b. Authenticate as admin
  c. GET /domiciliario → assert it's HTML (not a 404 or redirect)
  d. GET /api/delivery/orders → assert seeded order is present with required fields
  e. Assert the response includes an order whose status allows the delivery buttons
  f. PATCH status to 'en_camino' → assert 2xx AND DB change
  g. PATCH status to 'en_puerta' → assert DB change
  h. PATCH status to 'entregado' → assert DB final state

MUTATION SANITY:
  After the test passes, temporarily making GET /api/delivery/orders return [] will
  cause step (d) to fail. That proves the test has real teeth.

No ANTHROPIC_API_KEY required — this is a pure operational dashboard test.
"""
from __future__ import annotations

import uuid
import asyncio

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    create_admin_token,
    seed_restaurant,
    truncate_e2e_data,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

CUSTOMER_PHONE = "573001112222"


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
async def test_domiciliario_page_renders_buttons(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full domiciliario operational dashboard test.

    Proves that:
    - /domiciliario HTML page is reachable (not 404 or empty)
    - /api/delivery/orders returns seeded order with required fields for the JS
      to render the action buttons
    - Status transitions (confirmado → en_camino → en_puerta → entregado) all
      result in DB changes
    - RLS does NOT block the admin from seeing their org's orders
    """
    pool = test_pool

    # ── Seed restaurant ──────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Domiciliario Test Restaurant",
        bot_number_raw="+570E2EDOMICIL",
        num_branches=0,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, org_id)

    # ── Seed a paid delivery order in 'confirmado' status ────────────────────────
    # This represents an order that came in from WhatsApp, was validated by caja,
    # and is now ready for the domiciliario to pick up.
    order_id = str(uuid.uuid4())
    with bypass_tenant_scope("e2e_domiciliario_setup"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO orders
                     (id, org_id, phone, bot_number, order_type, status,
                      paid, total, subtotal, items, address)
                   VALUES ($1, $2, $3, $4, 'domicilio', 'confirmado', true,
                           35000, 35000, '[{"name":"Bandeja","qty":1,"price":35000}]'::jsonb,
                           'Calle 80 #10-42, Bogotá')""",
                order_id, org_id, CUSTOMER_PHONE, bot_number,
            )

    log.info(
        "e2e.domiciliario.setup_done",
        org_id=org_id,
        order_id=order_id,
        bot_number=bot_number,
    )

    # ── Admin auth ───────────────────────────────────────────────────────────────
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # ── Step c: GET /domiciliario → HTML ─────────────────────────────────────────
    page_resp = await e2e_app.get("/domiciliario", headers=auth_headers)
    assert page_resp.status_code == 200, (
        f"GET /domiciliario returned {page_resp.status_code}. "
        "The page must serve the domiciliario HTML — if it's 404, the route is missing."
    )
    content_type = page_resp.headers.get("content-type", "")
    assert "html" in content_type, (
        f"GET /domiciliario returned content-type={content_type!r} instead of HTML. "
        "The page is not rendering HTML — check dashboard.py route."
    )
    html_body = page_resp.text
    # The page must have some structure — a completely empty page means the template is broken
    assert len(html_body) > 200, (
        f"GET /domiciliario returned only {len(html_body)} chars — page appears empty. "
        "Check that domiciliario.html exists and is properly served."
    )
    log.info("e2e.domiciliario.page_ok", content_length=len(html_body))

    # ── Step d: GET /api/delivery/orders → assert order present ─────────────────
    orders_resp = await e2e_app.get("/api/delivery/orders", headers=auth_headers)
    assert orders_resp.status_code == 200, (
        f"GET /api/delivery/orders returned {orders_resp.status_code}: {orders_resp.text}. "
        "This is the data feed for the domiciliario page. Without it, the page is empty."
    )
    orders_data = orders_resp.json()
    orders_list = orders_data.get("orders", [])

    # LOAD-BEARING ASSERTION: the seeded order must appear in the response.
    # If this fails, the page renders flat (no data, no buttons). This is exactly
    # the regression the user reported.
    our_order = next(
        (o for o in orders_list if str(o.get("id")) == str(order_id)), None
    )
    assert our_order is not None, (
        f"Seeded order {order_id} NOT found in /api/delivery/orders response. "
        f"Found {len(orders_list)} orders: {[o.get('id') for o in orders_list]}. "
        "This means the domiciliario page would render FLAT (no buttons). "
        "Check RLS scope, org_id filter, and that get_delivery_orders uses the right org_id."
    )
    log.info(
        "e2e.domiciliario.order_found_in_api",
        order_id=order_id,
        status=our_order.get("status"),
        total=our_order.get("total"),
    )

    # ── Step e: "render proof" — JS needs these fields to show buttons ────────────
    # The domiciliario JS renders buttons based on these fields:
    #   - id: to build the PATCH URL
    #   - status: to decide which buttons to show
    #   - phone: to show customer contact
    #   - total: to show the amount
    # If any of these are missing, the JS silently fails to render buttons.
    assert our_order.get("id"), "Order is missing 'id' field — JS cannot build PATCH URL"
    assert our_order.get("status"), "Order is missing 'status' field — JS cannot decide buttons"
    assert our_order.get("total") is not None, "Order is missing 'total' field — JS shows blank amount"

    # LOAD-BEARING: assert the order's status is one that the JS will render buttons for.
    # The domiciliario.html renders "Salir a entregar" only for 'confirmado'/'en_preparacion'.
    assert our_order["status"] in ("confirmado", "en_preparacion", "listo"), (
        f"Order status is '{our_order['status']}' — not in the set that renders buttons. "
        "The page would show the order but with no actionable buttons."
    )

    # ── Step f: PATCH confirmado → en_camino ─────────────────────────────────────
    log.info("e2e.domiciliario.patch_en_camino", order_id=order_id)
    camino_resp = await e2e_app.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "en_camino"},
        headers=auth_headers,
    )
    assert camino_resp.status_code == 200, (
        f"PATCH to 'en_camino' failed: {camino_resp.status_code} {camino_resp.text}. "
        "This is the 'Salir a entregar' button action on the domiciliario page."
    )

    # Verify DB changed
    with bypass_tenant_scope("e2e_domiciliario_assert_camino"):
        async with pool.acquire() as conn:
            status_row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1", order_id,
            )
    assert status_row is not None, f"Order {order_id} disappeared after en_camino patch"
    assert status_row["status"] == "en_camino", (
        f"DB status after 'en_camino' PATCH: expected 'en_camino', got '{status_row['status']}'. "
        "The PATCH endpoint returned 200 but didn't update the DB — data loss bug."
    )
    log.info("e2e.domiciliario.camino_confirmed_in_db")

    # ── Step g: PATCH en_camino → en_puerta ─────────────────────────────────────
    log.info("e2e.domiciliario.patch_en_puerta", order_id=order_id)
    puerta_resp = await e2e_app.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "en_puerta"},
        headers=auth_headers,
    )
    assert puerta_resp.status_code == 200, (
        f"PATCH to 'en_puerta' failed: {puerta_resp.status_code} {puerta_resp.text}. "
        "This is the 'Llegué' button on the domiciliario page."
    )

    with bypass_tenant_scope("e2e_domiciliario_assert_puerta"):
        async with pool.acquire() as conn:
            status_row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1", order_id,
            )
    assert status_row["status"] == "en_puerta", (
        f"DB status after 'en_puerta' PATCH: expected 'en_puerta', got '{status_row['status']}'"
    )
    log.info("e2e.domiciliario.puerta_confirmed_in_db")

    # ── Step h: PATCH en_puerta → entregado ─────────────────────────────────────
    log.info("e2e.domiciliario.patch_entregado", order_id=order_id)
    entregado_resp = await e2e_app.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "entregado"},
        headers=auth_headers,
    )
    assert entregado_resp.status_code == 200, (
        f"PATCH to 'entregado' failed: {entregado_resp.status_code} {entregado_resp.text}. "
        "This is the 'Entregado' button on the domiciliario page."
    )

    # Give asyncio tasks a moment to fire (NPS trigger, WA notification)
    await asyncio.sleep(0.3)

    with bypass_tenant_scope("e2e_domiciliario_assert_entregado"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                "SELECT status, total FROM orders WHERE id = $1", order_id,
            )
    assert final_row is not None, f"Order {order_id} disappeared after entregado patch"
    assert final_row["status"] == "entregado", (
        f"DB final status: expected 'entregado', got '{final_row['status']}'. "
        "Full delivery lifecycle did not complete correctly."
    )
    # LOAD-BEARING: assert total is the seeded value (35000)
    from app.services.money import to_decimal
    from decimal import Decimal
    final_total = to_decimal(final_row["total"])
    assert final_total == Decimal("35000"), (
        f"Order total was mutated during status transitions: expected 35000, got {final_total}. "
        "A status transition must NEVER change the order total."
    )
    log.info(
        "e2e.domiciliario.test_passed",
        order_id=order_id,
        final_status=final_row["status"],
        final_total=str(final_total),
    )
    print(
        f"\n[OK] Domiciliario E2E test passed: order {order_id} "
        f"completed full delivery button flow (confirmado -> en_camino -> en_puerta -> entregado). "
        f"Total: {final_total} COP."
    )
