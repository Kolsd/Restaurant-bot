"""
tests/e2e/test_salon_split_checks_lifecycle.py -- E2E test: split checks + tip enforcement.

Full flow:
  1. Seed restaurant with 2 dishes at different prices (Empanaditas 15000 + Ceviche 25000)
  2. Create table via POST /api/tables
  3. Customer scans QR (opens dine-in session)
  4. Orders exactly 2 empanaditas + 1 ceviche → total must be 55000
  5. Kitchen flow: en_preparacion → listo → entregado
  6. Caja: create 2 checks (split: 40000 + 15000)
  7. Adversarial: tip cap probe on check 1 (>50% = 400/422 REQUIRED)
  8. Pay check 1 with valid tip (5000 = 12.5% of 40000)
  9. Assert partial: check 1 paid, check 2 still open, session still active
 10. Pay check 2 with valid tip (3000 = 20% of 15000)
 11. Assert terminal: both checks paid → factura_entregada, session != active

Tip-cap rule (tables.py line 1206):
  tip_amount_d > money_mul(tip_cap_base, Decimal("0.5")) → HTTP 400

Anti-permissiveness:
  - total == Decimal("55000") EXACT (not >=)
  - tip cap probe MUST return 400 or 422; 200 is a failing bug
  - no OR-chains on money assertions
  - no try/except around adversarial probe

bypass_tenant_scope reasons used in assertions:
  - "e2e_split_assert_session"  (verify session open/closed)
  - "e2e_split_assert_order"    (verify order row + status)
  - "e2e_split_assert_checks"   (verify check rows in table_checks)

Prerequisites:
  ANTHROPIC_API_KEY   -- real Anthropic key
  TEST_DATABASE_URL   -- Postgres URL pointing at a dedicated test DB

Run:
  pytest tests/e2e/test_salon_split_checks_lifecycle.py -v -s -m e2e --tb=short
"""
from __future__ import annotations

import asyncio
import json as _json
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
from app.services.money import to_decimal
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# -- Test constants -------------------------------------------------------------

CUSTOMER_PHONE_RAW = "+573009997766"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

MENU = {
    "Entradas": [
        {
            "name": "Empanaditas de Carne",
            "description": "Empanadas fritas rellenas de carne y papa",
            "price": 15000,
            "active": True,
        }
    ],
    "Platos Fuertes": [
        {
            "name": "Ceviche Especial",
            "description": "Ceviche de camarón con leche de tigre",
            "price": 25000,
            "active": True,
        }
    ],
}

PAYMENT_METHODS = ["Nequi", "Efectivo"]

# Expected financials (EXACT — any deviation = bot bug)
EXPECTED_TOTAL = Decimal("55000")   # 2×15000 + 1×25000
CHECK1_TOTAL   = Decimal("40000")   # 1 empanadita + 1 ceviche
CHECK2_TOTAL   = Decimal("15000")   # 1 empanadita
TIP1           = Decimal("5000")    # 12.5% of 40000 → within 50% cap
TIP2           = Decimal("3000")    # 20% of 15000  → within 50% cap
TIP_ADVERSARIAL = 25000.0           # >50% of 40000 = 20001 → must be rejected


# -- App fixture (function-scoped) --------------------------------------------

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


# -- The E2E test --------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_salon_split_checks_with_tips(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full split-check + tip-enforcement lifecycle:
    QR scan → order 55000 → kitchen → 2 checks (40k + 15k) → tip cap probe (→400)
    → pay check 1 (tip 5000) → pay check 2 (tip 3000) → factura_entregada.
    """
    client = e2e_app
    pool = test_pool

    # -- Seed restaurant -------------------------------------------------------
    restaurant = await seed_restaurant(
        pool,
        name="E2E Split Checks Test Restaurant",
        bot_number_raw="+570E2ESPLIT1",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    parent_id   = restaurant["id"]
    bot_number  = restaurant["whatsapp_number"]
    branch_1    = restaurant["branches"][0]
    branch_1_id = branch_1["id"]

    await truncate_e2e_data(pool, parent_id)
    await truncate_e2e_data(pool, branch_1_id)

    # Clear in-process state fallback
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info("e2e.split_test_start", parent_id=parent_id, branch_1_id=branch_1_id)

    # -- Admin auth -----------------------------------------------------------
    owner_email   = restaurant["owner_email"]
    admin_token   = await create_admin_token(pool, owner_email)
    auth_headers  = {"Authorization": f"Bearer {admin_token}"}
    branch_headers = {**auth_headers, "X-Branch-ID": str(branch_1_id)}

    # -- Create table ---------------------------------------------------------
    create_table_resp = await client.post("/api/tables", headers=branch_headers)
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_data = create_table_resp.json()
    table_id: str = table_data["table_id"]
    log.info("e2e.split_table_created", table_id=table_id)

    # =====================================================================
    # Turn 1: QR scan → opens dine-in session
    # =====================================================================
    qr_message = f"Hola, acabo de llegar [table_id:{table_id}]"
    processed_1 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=qr_message, bot_number=bot_number
    )
    assert processed_1 >= 1, "Turn 1: inbox item not processed"

    # Assert table_sessions row created
    session_row = None
    for _ in range(10):
        with bypass_tenant_scope("e2e_split_assert_session"):
            async with pool.acquire() as conn:
                session_row = await conn.fetchrow(
                    """
                    SELECT id, status FROM table_sessions
                    WHERE phone = $1 AND table_id = $2 AND status = 'active'
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE, table_id,
                )
        if session_row:
            break
        await asyncio.sleep(0.5)

    assert session_row is not None, (
        f"No active table_session for phone={CUSTOMER_PHONE} table_id={table_id}"
    )
    log.info("e2e.split_session_open", status=session_row["status"])

    # =====================================================================
    # Turn 2: Order EXACTLY 2 empanaditas + 1 ceviche (total must be 55000)
    # Use explicit language to minimise LLM ambiguity.
    # =====================================================================
    order_text = (
        "Quiero pedir exactamente 2 empanaditas de carne y 1 ceviche especial por favor"
    )
    processed_2 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text=order_text, bot_number=bot_number
    )
    assert processed_2 >= 1, "Turn 2: inbox item not processed"

    # =====================================================================
    # Turn 3: Confirm order
    # =====================================================================
    processed_3 = await simulate_whatsapp_inbound(
        client, pool, phone=CUSTOMER_PHONE_RAW, text="sí confirmo", bot_number=bot_number
    )
    assert processed_3 >= 1, "Turn 3: confirmation not processed"

    # -- Wait for table_order row ------------------------------------------
    order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_split_assert_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, phone, table_id, status, total, items, org_id,
                           location_id, base_order_id
                    FROM table_orders
                    WHERE phone = $1
                      AND table_id = $2
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE, table_id,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No table_order found for phone={CUSTOMER_PHONE} table_id={table_id}. "
        "Bot may not have called place_order or it failed to commit."
    )

    order_id: str      = order_row["id"]
    base_order_id: str = order_row["base_order_id"] or order_id
    order_total        = to_decimal(order_row["total"])
    items_raw          = order_row["items"]
    if isinstance(items_raw, str):
        items_raw = _json.loads(items_raw)
    items_raw = items_raw or []

    log.info(
        "e2e.split_order_found",
        base_order_id=base_order_id,
        status=order_row["status"],
        total=str(order_total),
        items_count=len(items_raw),
    )

    # EXACT total assertion — 2×15000 + 1×25000 = 55000. No OR-chain.
    assert order_total == EXPECTED_TOTAL, (
        f"Expected table_order total == {EXPECTED_TOTAL}, got {order_total}. "
        "The bot ordered the wrong quantity. This is a bot bug, not a test flaw. "
        f"Items: {items_raw}"
    )
    assert len(items_raw) == 2, (
        f"Expected 2 distinct dishes in items, got {len(items_raw)}: {items_raw}"
    )

    # =====================================================================
    # Kitchen transitions: en_preparacion → listo → entregado
    # (Rule: mandatory — unlocks POST /checks in production flow)
    # =====================================================================
    for new_status in ("en_preparacion", "listo", "entregado"):
        status_resp = await client.post(
            f"/api/table-orders/{base_order_id}/status",
            json={"status": new_status},
            headers=auth_headers,
        )
        assert status_resp.status_code == 200, (
            f"Status transition to {new_status} failed: "
            f"{status_resp.status_code} {status_resp.text}"
        )
    await asyncio.sleep(1.0)

    # =====================================================================
    # Build item name map from order rows for check payload construction
    # =====================================================================
    name_emp = "Empanaditas de Carne"
    name_cev = "Ceviche Especial"

    # Normalize actual names from the bot's order (may differ in accent/case)
    actual_names: dict[str, str] = {}
    for itm in items_raw:
        n = itm.get("name", "")
        if "empanadita" in n.lower() or "carne" in n.lower():
            actual_names["emp"] = n
        elif "ceviche" in n.lower():
            actual_names["cev"] = n

    name_emp = actual_names.get("emp", name_emp)
    name_cev = actual_names.get("cev", name_cev)

    # =====================================================================
    # Create 2 checks (split):
    #   Check 1: 1 empanadita (15000) + 1 ceviche (25000) = 40000
    #   Check 2: 1 empanadita (15000)
    # Tax = 0% so subtotal == total == gross price
    # =====================================================================
    checks_payload = {
        "checks": [
            {
                "check_number": 1,
                "items": [
                    {"name": name_emp, "qty": 1, "unit_price": 15000.0},
                    {"name": name_cev, "qty": 1, "unit_price": 25000.0},
                ],
            },
            {
                "check_number": 2,
                "items": [
                    {"name": name_emp, "qty": 1, "unit_price": 15000.0},
                ],
            },
        ],
        "tax_pct": 0.0,
        "tax_regime": "iva",
    }

    create_checks_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks",
        json=checks_payload,
        headers=auth_headers,
    )
    assert create_checks_resp.status_code == 200, (
        f"POST /checks failed: {create_checks_resp.status_code} {create_checks_resp.text}"
    )
    checks_data = create_checks_resp.json()
    checks_list = checks_data.get("checks", [])
    assert len(checks_list) == 2, (
        f"Expected 2 checks in response, got {len(checks_list)}: {checks_data}"
    )

    # Identify check 1 and check 2 by total
    check_by_total = {to_decimal(c["total"]): c for c in checks_list}
    assert CHECK1_TOTAL in check_by_total, (
        f"No check with total {CHECK1_TOTAL} found. Checks: {[(c['id'], c['total']) for c in checks_list]}"
    )
    assert CHECK2_TOTAL in check_by_total, (
        f"No check with total {CHECK2_TOTAL} found. Checks: {[(c['id'], c['total']) for c in checks_list]}"
    )
    check1_id: str = check_by_total[CHECK1_TOTAL]["id"]
    check2_id: str = check_by_total[CHECK2_TOTAL]["id"]

    log.info(
        "e2e.split_checks_created",
        check1_id=check1_id,
        check1_total=str(CHECK1_TOTAL),
        check2_id=check2_id,
        check2_total=str(CHECK2_TOTAL),
    )

    # =====================================================================
    # ADVERSARIAL: tip cap probe on check 1 BEFORE paying it.
    # 25000 > 50% of 40000 (= 20000) → MUST return 400.
    # If 200 → production bug (let the test fail loudly — no try/except).
    # =====================================================================
    log.info(
        "e2e.split_tip_cap_probe",
        check1_id=check1_id,
        adversarial_tip=TIP_ADVERSARIAL,
        cap=float(CHECK1_TOTAL) * 0.5,
    )
    tip_cap_probe_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks/{check1_id}/pay",
        json={
            "payments": [{"method": "Nequi", "amount": float(CHECK1_TOTAL)}],
            "tip_amount": TIP_ADVERSARIAL,
            "service_charge": 0.0,
            "customer_name": "E2E Tip Cap Test",
            "customer_nit": "222222222",
            "customer_email": "",
        },
        headers=auth_headers,
    )
    assert tip_cap_probe_resp.status_code in (400, 422), (
        f"Tip cap NOT enforced: expected 400 or 422, got {tip_cap_probe_resp.status_code}. "
        f"tip_amount={TIP_ADVERSARIAL} > 50% of {CHECK1_TOTAL} = {float(CHECK1_TOTAL) * 0.5}. "
        f"Response body: {tip_cap_probe_resp.text}. "
        "This is a PRODUCTION BUG — the tip cap validation in tables.py is broken."
    )
    log.info(
        "e2e.split_tip_cap_enforced",
        status_code=tip_cap_probe_resp.status_code,
        detail=tip_cap_probe_resp.json().get("detail", ""),
    )

    # =====================================================================
    # Pay check 1: Nequi, amount=40000, tip=5000 (12.5% — valid)
    # =====================================================================
    pay1_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks/{check1_id}/pay",
        json={
            "payments": [{"method": "Nequi", "amount": float(CHECK1_TOTAL)}],
            "tip_amount": float(TIP1),
            "service_charge": 0.0,
            "customer_name": "E2E Customer",
            "customer_nit": "222222222",
            "customer_email": "",
        },
        headers=auth_headers,
    )
    assert pay1_resp.status_code == 200, (
        f"Pay check 1 failed: {pay1_resp.status_code} {pay1_resp.text}"
    )
    pay1_data = pay1_resp.json()
    assert pay1_data.get("success") is True, (
        f"pay_check 1 did not return success=true: {pay1_data}"
    )
    log.info("e2e.split_check1_paid", change=pay1_data.get("change"))

    # -- Assert check 1 tip_amount in DB -----------------------------------
    with bypass_tenant_scope("e2e_split_assert_checks"):
        async with pool.acquire() as conn:
            check1_row = await conn.fetchrow(
                "SELECT total, tip_amount, status FROM table_checks WHERE id = $1",
                check1_id,
            )

    assert check1_row is not None, f"table_checks row for check1_id={check1_id} not found"
    assert to_decimal(check1_row["tip_amount"]) == TIP1, (
        f"Expected check 1 tip_amount == {TIP1}, got {to_decimal(check1_row['tip_amount'])}"
    )
    log.info(
        "e2e.split_check1_db_verified",
        tip_amount=str(to_decimal(check1_row["tip_amount"])),
        status=check1_row["status"],
    )

    # -- Assert table_orders NOT terminal yet ------------------------------
    with bypass_tenant_scope("e2e_split_assert_order"):
        async with pool.acquire() as conn:
            partial_order_row = await conn.fetchrow(
                "SELECT status FROM table_orders WHERE id = $1",
                base_order_id,
            )

    assert partial_order_row is not None
    assert partial_order_row["status"] != "factura_entregada", (
        f"table_order became 'factura_entregada' after only check 1 paid "
        f"(check 2 still open). Status: {partial_order_row['status']}"
    )
    log.info(
        "e2e.split_partial_order_status",
        status=partial_order_row["status"],
        note="check 2 still open — factura_entregada must NOT appear yet",
    )

    # -- Assert session still active (check 2 open) ------------------------
    with bypass_tenant_scope("e2e_split_assert_session"):
        async with pool.acquire() as conn:
            session_partial = await conn.fetchrow(
                """
                SELECT status FROM table_sessions
                WHERE phone = $1 AND table_id = $2
                ORDER BY started_at DESC LIMIT 1
                """,
                CUSTOMER_PHONE, table_id,
            )

    assert session_partial is not None
    assert session_partial["status"] == "active", (
        f"Session should still be 'active' while check 2 is open, "
        f"got '{session_partial['status']}'"
    )
    log.info("e2e.split_session_still_active", status=session_partial["status"])

    # =====================================================================
    # Pay check 2: Efectivo, amount=20000 (change 5000), tip=3000 (20% valid)
    # =====================================================================
    pay2_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks/{check2_id}/pay",
        json={
            "payments": [{"method": "Efectivo", "amount": 20000.0}],
            "tip_amount": float(TIP2),
            "service_charge": 0.0,
            "customer_name": "E2E Customer 2",
            "customer_nit": "222222222",
            "customer_email": "",
        },
        headers=auth_headers,
    )
    assert pay2_resp.status_code == 200, (
        f"Pay check 2 failed: {pay2_resp.status_code} {pay2_resp.text}"
    )
    pay2_data = pay2_resp.json()
    assert pay2_data.get("success") is True, (
        f"pay_check 2 did not return success=true: {pay2_data}"
    )
    log.info("e2e.split_check2_paid", change=pay2_data.get("change"))

    # -- Assert check 2 tip_amount in DB -----------------------------------
    with bypass_tenant_scope("e2e_split_assert_checks"):
        async with pool.acquire() as conn:
            check2_row = await conn.fetchrow(
                "SELECT total, tip_amount, status FROM table_checks WHERE id = $1",
                check2_id,
            )

    assert check2_row is not None, f"table_checks row for check2_id={check2_id} not found"
    assert to_decimal(check2_row["tip_amount"]) == TIP2, (
        f"Expected check 2 tip_amount == {TIP2}, got {to_decimal(check2_row['tip_amount'])}"
    )
    log.info(
        "e2e.split_check2_db_verified",
        tip_amount=str(to_decimal(check2_row["tip_amount"])),
        status=check2_row["status"],
    )

    # -- Assert total tips = TIP1 + TIP2 = 8000 ----------------------------
    with bypass_tenant_scope("e2e_split_assert_checks"):
        async with pool.acquire() as conn:
            tips_sum_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(tip_amount), 0) AS total_tips
                FROM table_checks
                WHERE base_order_id = $1
                """,
                base_order_id,
            )

    total_tips = to_decimal(tips_sum_row["total_tips"])
    expected_total_tips = TIP1 + TIP2  # 8000
    assert total_tips == expected_total_tips, (
        f"Expected total tips == {expected_total_tips} (TIP1={TIP1} + TIP2={TIP2}), "
        f"got {total_tips}"
    )
    log.info("e2e.split_total_tips_verified", total_tips=str(total_tips))

    # -- Assert table_orders is terminal (factura_entregada) ---------------
    final_order_row = None
    for _ in range(20):
        with bypass_tenant_scope("e2e_split_assert_order"):
            async with pool.acquire() as conn:
                final_order_row = await conn.fetchrow(
                    "SELECT status FROM table_orders WHERE id = $1",
                    base_order_id,
                )
        if final_order_row and final_order_row["status"] in (
            "factura_entregada", "cerrar_mesa", "closed"
        ):
            break
        await asyncio.sleep(0.5)

    assert final_order_row is not None
    assert final_order_row["status"] == "factura_entregada", (
        f"Expected table_orders.status='factura_entregada' after all checks paid, "
        f"got '{final_order_row['status']}'. "
        "db_finalize_check_payment must mark the order terminal once invoiced/cancelled "
        "covers every check."
    )
    log.info("e2e.split_order_terminal", status=final_order_row["status"])

    # -- Assert session is no longer active --------------------------------
    await asyncio.sleep(1.0)
    with bypass_tenant_scope("e2e_split_assert_session"):
        async with pool.acquire() as conn:
            session_after = await conn.fetchrow(
                """
                SELECT status FROM table_sessions
                WHERE phone = $1 AND table_id = $2
                ORDER BY started_at DESC LIMIT 1
                """,
                CUSTOMER_PHONE, table_id,
            )

    assert session_after is not None
    assert session_after["status"] != "active", (
        f"Session should be closed/nps_pending after both checks paid, "
        f"got status='{session_after['status']}'"
    )
    log.info("e2e.split_session_closed", status=session_after["status"])

    # -- Final summary --------------------------------------------------------
    all_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.split_test_passed",
        base_order_id=base_order_id,
        order_total=str(order_total),
        check1_id=check1_id,
        check2_id=check2_id,
        tip1=str(TIP1),
        tip2=str(TIP2),
        total_tips=str(total_tips),
        final_order_status=final_order_row["status"],
        session_status=session_after["status"],
        wa_messages=len(wa_capture.messages),
    )
    print(
        f"\n[OK] E2E split-checks test passed: "
        f"order {base_order_id} (total={EXPECTED_TOTAL}), "
        f"2 checks ({CHECK1_TOTAL}+{CHECK2_TOTAL}), "
        f"tip cap enforced, tips recorded ({TIP1}+{TIP2}={expected_total_tips}), "
        f"order={final_order_row['status']}, session={session_after['status']}. "
        f"{len(wa_capture.messages)} WA messages captured."
    )
