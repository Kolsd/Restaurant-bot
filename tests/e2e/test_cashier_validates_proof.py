"""
tests/e2e/test_caja_validates_proof.py — E2E test: caja validates Nequi proof.

Goal: customer paid via Nequi/Bancolombia and uploaded a proof image.
The caja operator sees the order in "Comprobantes" tab, inspects the image,
and clicks "Validar" to confirm the payment.

Steps:
  1. Seed org + delivery order with status 'pendiente' + proof_url set
  2. Caja login (admin auth)
  3. GET /api/delivery/orders → assert order is present with proof_url
  4. POST /api/delivery/orders/{id}/validate → assert 2xx
  5. DB assert: orders.paid = true, status = 'confirmado'
  6. Verify idempotency: calling validate a second time returns success (not 5xx)

No ANTHROPIC_API_KEY required — this is a caja dashboard test.
"""
from __future__ import annotations

import uuid

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
from app.services.money import to_decimal
from decimal import Decimal

log = get_logger(__name__)

CUSTOMER_PHONE = "573003334444"
ORDER_TOTAL = Decimal("47500")


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture, monkeypatch):
    """
    App fixture that patches loyalty accrual and WA notification to avoid
    depending on loyalty_customers schema being seeded or Meta credentials.
    """
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager
    from unittest.mock import AsyncMock
    from app.routes import orders_routes as ord_routes

    # Patch send_delivery_notification to avoid needing Meta credentials
    monkeypatch.setattr(
        ord_routes, "send_delivery_notification", AsyncMock(return_value=None)
    )
    # Patch loyalty accrual (counted, not executed — loyalty_customers may not be seeded)
    monkeypatch.setattr(
        ord_routes, "db_accrue_loyalty_points", AsyncMock(return_value=0)
    )

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
async def test_caja_validates_proof(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Caja proof validation flow:

    Seed order with proof → list in delivery orders → validate → assert paid=true.

    This test proves the entire "Comprobantes" tab of the caja page works end-to-end:
    - The order is visible in the comprobantes list (GET /api/delivery/orders)
    - The validate endpoint (POST .../validate) marks it paid and confirmed
    - The DB reflects paid=true, status='confirmado'
    - A second validate call is idempotent (no 500)
    """
    pool = test_pool

    # ── Seed restaurant ──────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Caja Test Restaurant",
        bot_number_raw="+570E2ECAJA",
        num_branches=0,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]
    owner_email = restaurant["owner_email"]

    await truncate_e2e_data(pool, org_id)

    # ── Seed pending delivery order with a proof_url ─────────────────────────────
    # This simulates: customer sent a Nequi screenshot, bot attached it via
    # db_attach_order_proof, and the order is now waiting for caja validation.
    order_id = str(uuid.uuid4())
    fake_proof_url = f"/api/media/img_{uuid.uuid4().hex[:12]}?bot={bot_number}"

    with bypass_tenant_scope("e2e_caja_proof_setup"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO orders
                     (id, org_id, phone, bot_number, order_type, status,
                      paid, total, subtotal, items, address, proof_url,
                      payment_method)
                   VALUES ($1, $2, $3, $4, 'domicilio', 'pendiente', false,
                           $5, $5, '[{"name":"Empanadas","qty":3,"price":15000}]'::jsonb,
                           'Calle 45 # 20-10, Bogotá', $6, 'Nequi')""",
                order_id, org_id, CUSTOMER_PHONE, bot_number,
                float(ORDER_TOTAL),
                fake_proof_url,
            )

    log.info(
        "e2e.caja.setup_done",
        org_id=org_id,
        order_id=order_id,
        proof_url=fake_proof_url,
    )

    # ── Admin auth ───────────────────────────────────────────────────────────────
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # ── Step 3: GET /api/delivery/orders → assert order in comprobantes ──────────
    orders_resp = await e2e_app.get("/api/delivery/orders", headers=auth_headers)
    assert orders_resp.status_code == 200, (
        f"GET /api/delivery/orders returned {orders_resp.status_code}: {orders_resp.text}"
    )
    orders_data = orders_resp.json()
    all_orders = orders_data.get("orders", [])

    our_order = next(
        (o for o in all_orders if str(o.get("id")) == str(order_id)), None
    )
    # LOAD-BEARING ASSERTION: order must appear in the list.
    # If this fails, the caja "Comprobantes" tab is empty and the cashier cannot validate.
    assert our_order is not None, (
        f"Order {order_id} NOT found in /api/delivery/orders response. "
        f"Got {len(all_orders)} orders: {[o.get('id') for o in all_orders]}. "
        "The 'Comprobantes' tab in caja would be empty — cashier cannot validate."
    )
    # The order must have a proof_url (the "Validar" button only appears when this is set)
    assert our_order.get("proof_url") or our_order.get("payment_proof_url"), (
        f"Order {order_id} in API response has no proof_url: {our_order!r}. "
        "The caja UI would not show the 'Validar' button without this field."
    )
    # LOAD-BEARING: verify total
    returned_total = to_decimal(our_order.get("total", 0))
    assert returned_total == ORDER_TOTAL, (
        f"Order total in API: expected {ORDER_TOTAL}, got {returned_total}. "
        "The caja would display the wrong amount to the cashier."
    )
    log.info(
        "e2e.caja.order_in_api",
        order_id=order_id,
        status=our_order.get("status"),
        total=float(returned_total),
        has_proof=bool(our_order.get("proof_url") or our_order.get("payment_proof_url")),
    )

    # ── Step 4: POST /api/delivery/orders/{id}/validate (caja clicks "Validar") ─
    validate_resp = await e2e_app.post(
        f"/api/delivery/orders/{order_id}/validate",
        json={"proof_media_id": "img_test", "notes": "Comprobante verificado E2E"},
        headers=auth_headers,
    )
    assert validate_resp.status_code == 200, (
        f"POST /validate returned {validate_resp.status_code}: {validate_resp.text}. "
        "This is the 'Validar' button on the caja Comprobantes tab. "
        "If it fails, cashier cannot confirm orders — all manual payments are blocked."
    )
    validate_data = validate_resp.json()
    assert validate_data.get("success") is True, (
        f"Validate response body did not have 'success': true — got {validate_data!r}"
    )
    log.info("e2e.caja.validate_ok", response=validate_data)

    # ── Step 5: DB assert paid=true, status='confirmado' ─────────────────────────
    with bypass_tenant_scope("e2e_caja_assert_paid"):
        async with pool.acquire() as conn:
            db_row = await conn.fetchrow(
                "SELECT paid, status, total FROM orders WHERE id = $1",
                order_id,
            )
    assert db_row is not None, f"Order {order_id} disappeared from DB after validate"

    # LOAD-BEARING ASSERTIONS — these prove the validate endpoint actually worked:
    assert db_row["paid"] is True, (
        f"orders.paid must be TRUE after validate, but got {db_row['paid']}. "
        "The payment confirmation did not write to DB."
    )
    assert db_row["status"] == "confirmado", (
        f"orders.status must be 'confirmado' after validate, but got '{db_row['status']}'. "
        "The status transition did not happen — order is stuck in '{db_row['status']}'."
    )
    # Verify total was not corrupted
    db_total = to_decimal(db_row["total"])
    assert db_total == ORDER_TOTAL, (
        f"orders.total changed after validate: expected {ORDER_TOTAL}, got {db_total}. "
        "A validation must NEVER change the order total."
    )
    log.info(
        "e2e.caja.db_state_after_validate",
        paid=db_row["paid"],
        status=db_row["status"],
        total=float(db_total),
    )

    # ── Step 6: Idempotency — second validate must succeed (not 500) ─────────────
    validate_again_resp = await e2e_app.post(
        f"/api/delivery/orders/{order_id}/validate",
        json={"proof_media_id": "img_test", "notes": "Re-validación idempotente"},
        headers=auth_headers,
    )
    assert validate_again_resp.status_code == 200, (
        f"Second POST /validate returned {validate_again_resp.status_code}. "
        "The validate endpoint must be idempotent — double-click from caja must not 500."
    )
    second_data = validate_again_resp.json()
    assert second_data.get("success") is True, (
        f"Second validate response did not have success=true: {second_data!r}"
    )
    # The second call must signal it was already paid (idempotent path)
    assert second_data.get("already_paid") is True, (
        f"Second validate did not return already_paid=true — got {second_data!r}. "
        "The endpoint may have re-processed an already-paid order (double accrual risk)."
    )
    log.info("e2e.caja.idempotency_ok", second_response=second_data)

    print(
        f"\n[OK] Caja proof validation E2E test passed: "
        f"order {order_id} confirmed (paid=true, status=confirmado, total={ORDER_TOTAL} COP). "
        f"Idempotency verified."
    )
