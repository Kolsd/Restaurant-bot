"""
tests/e2e/test_salon_qr_claim_lifecycle.py — E2E: full salon QR-Phone-Claim lifecycle.

Variante del flujo en test_salon_qr_lifecycle.py que usa Path 0 (QR-Phone-Claim,
Capa 1 deployada 2026-04-28) en lugar del marker [table_id:X] de Path 1.

Diferencia clave vs test_salon_qr_lifecycle.py:
  - ANTES del primer WhatsApp: POST /api/qr-claim registra phone+table_id.
  - El primer mensaje WhatsApp del cliente es LIMPIO ("Hola, quisiera el menú 👋").
  - detect_table_context Path 0 consume el claim y abre la sesión correctamente.
  - El mensaje NO contiene [table_id:X] ni ningún marcador técnico.

Adicionalmente cubre 3 asserts de disconnects identificados en
docs/PRODUCT_CONTEXT.md regla #13 (tabla "Los 10 disconnects descubiertos hoy"
en SESSION_HANDOFF_2026-04-28.md) que el test viejo NO tiene:

  DISCONNECT #2 — Mesa ↔ Mesero:
    table_sessions.assigned_staff_id debe ser NOT NULL después de abrir la sesión.
    Hoy ningún flujo asigna mesero automáticamente al abrir mesa.
    → FALLA en main actual. Es el roadmap concreto para la sub-sesión 0b.

  DISCONNECT #3 — Cocina ↔ Mesero:
    Cuando cocina marca status=listo, debe crearse un waiter_alert con
    alert_type='ready' (o similar) para notificar al mesero asignado.
    Hoy no se crea ningún waiter_alert al marcar listo.
    → FALLA en main actual. Es el roadmap concreto para la sub-sesión 0c.

  DISCONNECT #8 — Mesa ↔ NPS:
    Inmediatamente después del pay_check (misma operación atómica), debe
    existir un row en nps_waiting para el phone del cliente. NPS debe estar
    encolado ANTES de que el cliente abandone la mesa, no después del timeout
    del scheduler.
    → PUEDE PASAR si trigger_nps ya corre dentro del pay_check flow
       (via _farewell_and_nps → trigger_nps → db_save_nps_waiting).
       Si falla, es timing/scope issue que bloquea la tasa de respuesta NPS.

ESPERADO: este test FALLA en main actual en los asserts DISCONNECT #2 y #3.
Eso es deseable: cada falla es el roadmap concreto para la sub-sesión que la arregla.

Run:
  pytest tests/e2e/test_salon_qr_claim_lifecycle.py -v -s -m e2e

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — Postgres URL (isolated, non-production)
  DISABLE_EMBEDDED_WORKER=1  — set automatically by conftest.py (test_pool fixture)
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

# Use a DISTINCT phone from test_salon_qr_lifecycle.py (+573009998877)
# so both tests can run in the same pytest session without state collisions.
CUSTOMER_PHONE_RAW = "+573001112233"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# A distinct bot number from the existing test to avoid org collisions.
BOT_NUMBER_RAW = "+570E2EQRCLAIM1"

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

PAYMENT_METHODS = ["Nequi", "Efectivo"]

# Bogotá zona norte — branch geo coords (same as the test restaurant branch)
BRANCH_LAT = 4.710989
BRANCH_LON = -74.072092


# ── App fixture (function-scoped) ─────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """
    Yields an httpx.AsyncClient wrapping the real FastAPI app via ASGI transport.

    The FastAPI lifespan is started (db pool init, scheduler, etc.) and torn down
    cleanly after each test.  DISABLE_EMBEDDED_WORKER=1 is set by test_pool so the
    background inbox loop does not compete with drain_inbox().

    NOTE: this fixture is intentionally identical to the one in
    test_salon_qr_lifecycle.py because pytest_asyncio fixtures cannot be
    re-exported cross-file without conftest.py changes.  See conftest.py note on
    "NO duplicar nada de eso" — this is the one unavoidable duplication justified
    by pytest's fixture scoping rules.
    """
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=120.0,
        ) as client:
            yield client


# ── The E2E test ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_salon_qr_claim_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full salon lifecycle using QR-Phone-Claim (Path 0 of detect_table_context).

    Etapas:
      1.  Seed restaurant (1 parent + 1 branch).
      2.  Seed staff role='mesero' in branch (INSERT directo).
      3.  Create table via POST /api/tables (admin).
      4.  POST /api/qr-claim (public, no auth) — phone+table_id+bot pre-bind.
      5.  First WhatsApp message: CLEAN text (no [table_id:X] marker).
      6.  ASSERT: table_sessions row is active.
      7.  ASSERT DISCONNECT #2: assigned_staff_id IS NOT NULL in table_sessions.
      8.  Turn 2: order food.
      9.  Turn 3: confirm.
      10. ASSERT: table_orders row created.
      11. POST status=en_preparacion (kitchen).
      12. POST status=listo (kitchen).
      13. ASSERT DISCONNECT #3: waiter_alerts row created for table (alert_type relevant).
      14. POST status=entregado (mesero).
      15. POST /api/table-orders/{base}/checks (caja creates check).
      16. POST /api/table-orders/{base}/checks/{id}/pay (caja pays Nequi).
      17. ASSERT: factura_entregada status.
      18. ASSERT DISCONNECT #8: nps_waiting row for phone exists post-pay.
    """
    client = e2e_app
    pool = test_pool

    # ── Etapa 1: Seed restaurant ───────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E QR Claim Test Restaurant",
        bot_number_raw=BOT_NUMBER_RAW,
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(BRANCH_LAT, BRANCH_LON)],
    )
    parent_id = restaurant["id"]        # org_id
    bot_number = restaurant["whatsapp_number"]   # normalized (no +)
    branch_1 = restaurant["branches"][0]
    branch_1_id = branch_1["id"]        # location_id

    await truncate_e2e_data(pool, parent_id)
    await truncate_e2e_data(pool, branch_1_id)

    # Reset in-process fallback state (NPS, checkout, cart locks, rate limits)
    try:
        import app.services.state_store as ss
        for attr in [
            "_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
            "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits",
        ]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info(
        "e2e.qr_claim_test_start",
        parent_id=parent_id,
        branch_1_id=branch_1_id,
        bot_number=bot_number,
    )

    # ── Etapa 2: Seed staff (mesero) for branch ────────────────────────────────
    # We need at least one mesero so DISCONNECT #2 assert is meaningful:
    # if assigned_staff_id is set, there must be a valid staff row to reference.
    with bypass_tenant_scope("e2e_seed_staff"):
        async with pool.acquire() as conn:
            staff_row = await conn.fetchrow(
                """
                INSERT INTO staff (
                    org_id, location_id, name, username, role, roles, pin,
                    active, hourly_rate
                )
                VALUES (
                    $1, $2,
                    'Mesero E2E', 'mesero.e2e.qr', 'mesero',
                    '["mesero"]'::jsonb, '0000',
                    true, 15000.00
                )
                ON CONFLICT (username) DO UPDATE
                    SET org_id = EXCLUDED.org_id,
                        location_id = EXCLUDED.location_id,
                        active = true
                RETURNING id
                """,
                parent_id,
                branch_1_id,
            )
    seeded_staff_id = staff_row["id"]
    log.info("e2e.qr_claim_staff_seeded", staff_id=str(seeded_staff_id))

    # ── Admin token ────────────────────────────────────────────────────────────
    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    branch_headers = {**auth_headers, "X-Branch-ID": str(branch_1_id)}

    # ── Etapa 3: Create table via admin API ───────────────────────────────────
    create_table_resp = await client.post(
        "/api/tables",
        headers=branch_headers,
    )
    assert create_table_resp.status_code == 200, (
        f"POST /api/tables failed: {create_table_resp.status_code} {create_table_resp.text}"
    )
    table_data = create_table_resp.json()
    table_id: str = table_data["table_id"]
    table_name: str = table_data.get("name", table_id)
    log.info("e2e.qr_claim_table_created", table_id=table_id, table_name=table_name)

    # ── Etapa 4: POST /api/qr-claim (public endpoint, no auth) ───────────────
    # This simulates the browser calling the endpoint after the customer typed
    # their phone number in the modal on /menu/{table_id}.
    qr_claim_body = {
        "bot_number": BOT_NUMBER_RAW,   # raw (with +) — endpoint normalizes it
        "table_id": table_id,
        "phone": CUSTOMER_PHONE_RAW,    # validator strips non-digits
        "geo_lat": BRANCH_LAT,          # exactly at the branch — geo_verified=true
        "geo_lon": BRANCH_LON,
    }
    claim_resp = await client.post("/api/qr-claim", json=qr_claim_body)
    assert claim_resp.status_code in (200, 201), (
        f"POST /api/qr-claim returned {claim_resp.status_code}: {claim_resp.text}. "
        "Check that: (a) bot_number resolves to the seeded restaurant, "
        "(b) phone passes the 7-15 digit validator, "
        "(c) table_id is a valid non-empty string."
    )
    claim_data = claim_resp.json()
    assert "claim_id" in claim_data or "ok" in claim_data, (
        f"POST /api/qr-claim response missing expected fields: {claim_data}"
    )
    log.info("e2e.qr_claim_created", claim_data=claim_data)

    # ── Etapa 5: First WhatsApp message — CLEAN (no marker) ───────────────────
    # This is the core difference from test_salon_qr_lifecycle.py.
    # Path 0 in detect_table_context must find the claim by phone+bot_number
    # and open the session without any [table_id:X] in the message.
    clean_message = "Hola, quisiera el menú 👋"
    log.info("e2e.qr_claim_turn_1", phone=CUSTOMER_PHONE, text=clean_message)
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=clean_message,
        bot_number=bot_number,
    )
    log.info(
        "e2e.qr_claim_turn_1_done",
        processed=processed_1,
        elapsed_s=round(time.monotonic() - t1_start, 1),
    )
    assert processed_1 >= 1, "Turn 1: inbox item was not processed"

    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.qr_claim_turn_1_replies",
        count=len(turn1_replies),
        replies=turn1_replies[:2],
    )
    assert len(turn1_replies) >= 1, (
        "Turn 1: bot sent no WA message after clean first message. "
        "Check ANTHROPIC_API_KEY and that detect_table_context Path 0 resolved the claim. "
        "If bot_number lookup failed (no restaurant for this bot_number), it silently drops the message."
    )

    # ── Etapa 6: ASSERT — table_sessions row created ──────────────────────────
    session_row = None
    for attempt in range(10):
        with bypass_tenant_scope("e2e_assert_session_created"):
            async with pool.acquire() as conn:
                session_row = await conn.fetchrow(
                    """
                    SELECT id, phone, table_id, status, assigned_staff_id
                    FROM table_sessions
                    WHERE phone = $1
                      AND table_id = $2
                      AND status = 'active'
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    table_id,
                )
        if session_row:
            break
        await asyncio.sleep(0.5)

    assert session_row is not None, (
        f"No active table_session found for phone={CUSTOMER_PHONE} table_id={table_id}. "
        "Likely Path 0 (QR-Phone-Claim) did NOT match the claim. "
        "Possible causes: (a) phone normalization mismatch (claim stored vs Meta-delivered phone), "
        "(b) bot_number mismatch between claim and webhook payload, "
        "(c) claim expired before the message arrived, "
        "(d) find_unclaimed_by_phone query did not find the row. "
        "Check qr_claims_repo._canonical_phone() normalization and the claim row in qr_scan_pending."
    )
    log.info(
        "e2e.qr_claim_session_confirmed",
        session_id=str(session_row["id"]),
        table_id=session_row["table_id"],
        status=session_row["status"],
        assigned_staff_id=str(session_row["assigned_staff_id"]),
    )

    # ── Etapa 7: ASSERT DISCONNECT #2 — Mesa ↔ Mesero ────────────────────────
    # When a new dine-in session is opened via QR-claim, the system should
    # automatically assign an available mesero (staff with role='mesero') at
    # that location. Currently NO flow does this assignment.
    #
    # TODO disconnect #2: implement auto-assignment in db_create_table_session
    # or in agent.detect_table_context after creating the session.  Options:
    #   (a) Round-robin assignment from staff WHERE role='mesero' AND location_id=$1 AND active=true
    #   (b) Load-balanced: pick mesero with fewest active table_sessions
    # File owners: app/repositories/tables_repo.py (db_create_table_session),
    #              app/services/agent.py (Path 0, post-session creation block).
    assert session_row["assigned_staff_id"] is not None, (
        "DISCONNECT #2: table_session.assigned_staff_id es NULL — "
        "ningún flujo asigna un mesero al abrir la mesa vía QR-Phone-Claim. "
        "El mesero no sabe qué mesa atender, el cliente no sabe quién le atiende, "
        "y NPS per-mesero queda sin attributable data. "
        "Sub-sesión objetivo: 0b (Mesa ↔ Mesero auto-assignment)."
    )

    # ── Etapa 8: Turn 2 — Order food ──────────────────────────────────────────
    order_text = "Quiero pedir 2 empanaditas de carne"
    log.info("e2e.qr_claim_turn_2", phone=CUSTOMER_PHONE, text=order_text)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=order_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.qr_claim_turn_2_done",
        processed=processed_2,
        elapsed_s=round(time.monotonic() - t2_start, 1),
    )
    assert processed_2 >= 1, "Turn 2: order inbox item was not processed"

    turn2_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(turn2_replies) >= 2, (
        "Turn 2: bot sent no reply after order attempt. "
        "Check that the menu has 'Empanaditas de Carne' and place_order tool was called."
    )

    # ── Etapa 9: Turn 3 — Confirm order ───────────────────────────────────────
    confirm_text = "sí confirmo"
    log.info("e2e.qr_claim_turn_3", phone=CUSTOMER_PHONE, text=confirm_text)
    t3_start = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client,
        pool,
        phone=CUSTOMER_PHONE_RAW,
        text=confirm_text,
        bot_number=bot_number,
    )
    log.info(
        "e2e.qr_claim_turn_3_done",
        processed=processed_3,
        elapsed_s=round(time.monotonic() - t3_start, 1),
    )
    assert processed_3 >= 1, "Turn 3: confirmation inbox item was not processed"

    # ── Etapa 10: ASSERT — table_orders row created ───────────────────────────
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_assert_table_order"):
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
                    CUSTOMER_PHONE,
                    table_id,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No table_order found for phone={CUSTOMER_PHONE} table_id={table_id}. "
        "Check that the bot called place_order and it committed to the DB. "
        "If the QR-claim session was not opened (Etapa 6 assert would have caught it), "
        "the bot may have fallen back to a delivery flow — check table_orders vs orders tables."
    )

    from app.services.money import to_decimal
    order_id: str = order_row["id"]
    base_order_id: str = order_row["base_order_id"] or order_id
    order_total = to_decimal(order_row["total"])
    log.info(
        "e2e.qr_claim_order_found",
        order_id=order_id,
        base_order_id=base_order_id,
        status=order_row["status"],
        total=str(order_total),
    )
    assert order_total >= Decimal("15000"), (
        f"Expected table_order total >= 15000 (at least 1 empanadita), got {order_total}."
    )

    # ── Etapa 11: Kitchen status — recibido → en_preparacion ─────────────────
    log.info("e2e.qr_claim_patch_status", order_id=base_order_id, status="en_preparacion")
    status_resp = await client.post(
        f"/api/table-orders/{base_order_id}/status",
        json={"status": "en_preparacion"},
        headers=auth_headers,
    )
    assert status_resp.status_code == 200, (
        f"POST status=en_preparacion failed: {status_resp.status_code} {status_resp.text}"
    )

    # ── Etapa 12: Kitchen status → listo ──────────────────────────────────────
    log.info("e2e.qr_claim_patch_status", order_id=base_order_id, status="listo")
    listo_resp = await client.post(
        f"/api/table-orders/{base_order_id}/status",
        json={"status": "listo"},
        headers=auth_headers,
    )
    assert listo_resp.status_code == 200, (
        f"POST status=listo failed: {listo_resp.status_code} {listo_resp.text}"
    )

    # ── Etapa 13: ASSERT DISCONNECT #3 — Cocina ↔ Mesero ─────────────────────
    # When kitchen marks an order as 'listo', a waiter_alert should be created
    # so the mesero assigned to that table knows the food is ready at the pass.
    # Currently, status transitions in POST /api/table-orders/{id}/status do NOT
    # create any waiter_alert when status='listo'. The mesero has to check the
    # /mesero screen manually (poll-based, not push-based).
    #
    # Expected alert_type: 'ready' or 'listo' or 'food_ready' — whichever is
    # implemented. We check for any waiter_alert for this table created after
    # the 'listo' transition, regardless of type, to be forward-compatible.
    #
    # TODO disconnect #3: in tables.py POST /api/table-orders/{id}/status,
    # when status transitions to 'listo', call db_create_waiter_alert with:
    #   alert_type='ready'
    #   table_id=table_id
    #   message=f"Pedido listo en el pase — Mesa {table_name}"
    # File owner: app/routes/tables.py (status transition block, ~line 868).
    listo_timestamp = time.time()
    # Brief wait to allow any async side-effect to commit
    await asyncio.sleep(0.5)

    waiter_alert_row = None
    with bypass_tenant_scope("e2e_assert_waiter_alert_listo"):
        async with pool.acquire() as conn:
            waiter_alert_row = await conn.fetchrow(
                """
                SELECT id, alert_type, message, dismissed
                FROM waiter_alerts
                WHERE table_id = $1
                  AND dismissed = FALSE
                  AND created_at > NOW() - INTERVAL '30 seconds'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                table_id,
            )

    assert waiter_alert_row is not None, (
        f"DISCONNECT #3: cocina marcó status=listo para mesa {table_id} pero "
        "NO se creó ningún waiter_alert. El mesero asignado no recibe notificación. "
        "La comida queda en el pase hasta que el mesero la vea por polling. "
        "Sub-sesión objetivo: 0c (Cocina ↔ Mesero alerta automática en 'listo')."
    )
    log.info(
        "e2e.qr_claim_waiter_alert_found",
        alert_id=str(waiter_alert_row["id"]),
        alert_type=waiter_alert_row["alert_type"],
        message=waiter_alert_row["message"],
    )

    # ── Etapa 14: Mesero — status → entregado ────────────────────────────────
    log.info("e2e.qr_claim_patch_status", order_id=base_order_id, status="entregado")
    entregado_resp = await client.post(
        f"/api/table-orders/{base_order_id}/status",
        json={"status": "entregado"},
        headers=auth_headers,
    )
    assert entregado_resp.status_code == 200, (
        f"POST status=entregado failed: {entregado_resp.status_code} {entregado_resp.text}"
    )
    await asyncio.sleep(1.0)  # allow 'entregado' WA notification task to run

    # ── Etapa 15: Caja — create check ────────────────────────────────────────
    items_raw = order_row["items"]
    if isinstance(items_raw, str):
        import json as _json
        items_raw = _json.loads(items_raw)
    items_raw = items_raw or []

    check_items = []
    for item in items_raw:
        check_items.append({
            "name": item["name"],
            "qty": int(item.get("qty", item.get("quantity", 1))),
            "unit_price": float(to_decimal(item.get("price", item.get("unit_price", 15000)))),
        })

    if not check_items:
        # Fallback: known values if items are stored differently
        check_items = [{"name": "Empanaditas de Carne (3 uds)", "qty": 2, "unit_price": 15000.0}]

    checks_payload = {
        "checks": [{"check_number": 1, "items": check_items}],
        "tax_pct": 0.0,
        "tax_regime": "iva",
    }
    create_checks_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks",
        json=checks_payload,
        headers=auth_headers,
    )
    assert create_checks_resp.status_code == 200, (
        f"POST checks failed: {create_checks_resp.status_code} {create_checks_resp.text}"
    )
    checks_data = create_checks_resp.json()
    checks_list = checks_data.get("checks", [])
    assert len(checks_list) >= 1, f"Expected >= 1 check in response, got: {checks_data}"
    check_id: str = checks_list[0]["id"]
    check_total = to_decimal(checks_list[0]["total"])
    log.info("e2e.qr_claim_check_created", check_id=check_id, check_total=str(check_total))

    # ── Etapa 16: Caja — pay check ───────────────────────────────────────────
    log.info("e2e.qr_claim_pay_check", check_id=check_id, amount=float(check_total))
    pay_resp = await client.post(
        f"/api/table-orders/{base_order_id}/checks/{check_id}/pay",
        json={
            "payments": [{"method": "Nequi", "amount": float(check_total)}],
            "tip_amount": 0.0,
            "service_charge": 0.0,
            "customer_name": "E2E QR Claim Customer",
            "customer_nit": "333333333",
            "customer_email": "",
        },
        headers=auth_headers,
    )
    assert pay_resp.status_code == 200, (
        f"POST pay_check failed: {pay_resp.status_code} {pay_resp.text}"
    )
    pay_data = pay_resp.json()
    assert pay_data.get("success") is True, (
        f"pay_check did not return success=true: {pay_data}"
    )
    log.info("e2e.qr_claim_check_paid", check_id=check_id, change=pay_data.get("change"))

    # ── Etapa 17: ASSERT — factura_entregada ──────────────────────────────────
    final_order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_assert_final_order_status"):
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

    assert final_order_row is not None, (
        f"table_order {base_order_id} disappeared from DB after pay_check"
    )
    assert final_order_row["status"] in ("factura_entregada", "cerrar_mesa", "closed"), (
        f"Expected table_order in terminal status after payment, "
        f"got '{final_order_row['status']}'"
    )
    log.info(
        "e2e.qr_claim_order_terminal",
        order_id=base_order_id,
        status=final_order_row["status"],
    )

    # ── Etapa 18: ASSERT DISCONNECT #8 — Mesa ↔ NPS ──────────────────────────
    # After pay_check (which calls _farewell_and_nps → trigger_nps), there should
    # be a row in nps_waiting for the customer's phone. This row is what survives
    # server restarts and allows the NPS to be delivered when the customer sends
    # the next WhatsApp message.
    #
    # Timing concern: _farewell_and_nps is called synchronously inside
    # _finalize_check_payment (tables.py), but trigger_nps uses state_store
    # (Redis/in-process). The DB write (db_save_nps_waiting → INSERT INTO nps_waiting)
    # happens AFTER state_store.nps_set. If Redis is down (in-process fallback mode),
    # the nps_waiting INSERT still happens inside the same call stack.
    #
    # If this assert fails: NPS is either (a) not triggered in the pay_check path,
    # (b) trigger_nps skips due to nps_is_done / lock contention, or
    # (c) db_save_nps_waiting's tenant_scope is wrong (no org_id resolved from bot).
    #
    # TODO disconnect #8 (if this assert fails): check _farewell_and_nps call site,
    # ensure trigger_nps is called with the correct bot_number (parent, not _b{n}),
    # and that db_save_nps_waiting resolves org_id from bot_number without error.
    # File owners: app/routes/tables.py (_farewell_and_nps),
    #              app/services/agent.py (trigger_nps),
    #              app/repositories/conversations_repo.py (db_save_nps_waiting).
    await asyncio.sleep(1.5)  # allow trigger_nps async side-effects to complete

    nps_waiting_row = None
    with bypass_tenant_scope("e2e_assert_nps_waiting"):
        async with pool.acquire() as conn:
            nps_waiting_row = await conn.fetchrow(
                """
                SELECT id, phone, bot_number, created_at
                FROM nps_waiting
                WHERE phone = $1
                  AND created_at > NOW() - INTERVAL '5 minutes'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                CUSTOMER_PHONE,
            )

    assert nps_waiting_row is not None, (
        f"DISCONNECT #8: NPS no se encoló en nps_waiting para phone={CUSTOMER_PHONE} "
        "después del pay_check. El cliente ya salió del restaurante y si el NPS no está "
        "encolado, nunca lo recibirá. "
        "Verificar: (a) _farewell_and_nps se llama en el pay_check path (tables.py), "
        "(b) trigger_nps resuelve bot_number limpio (sin sufijo _b), "
        "(c) db_save_nps_waiting inserta con org_id correcto bajo tenant_scope. "
        "Sub-sesión objetivo: 0h (Mesa ↔ NPS timing)."
    )
    log.info(
        "e2e.qr_claim_nps_waiting_confirmed",
        nps_id=str(nps_waiting_row["id"]),
        phone=nps_waiting_row["phone"],
        bot_number=nps_waiting_row["bot_number"],
    )

    # ── Final summary ──────────────────────────────────────────────────────────
    all_customer_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info(
        "e2e.qr_claim_test_passed",
        base_order_id=base_order_id,
        check_id=check_id,
        order_total=str(order_total),
        final_order_status=final_order_row["status"],
        total_wa_messages=len(wa_capture.messages),
        customer_wa_messages=len(all_customer_texts),
    )
    print(
        f"\n[OK] E2E QR-Phone-Claim test passed: order {base_order_id} completed full "
        f"dine-in lifecycle (QR-claim→clean-message→Path0→order→kitchen→pay_check→NPS). "
        f"{len(wa_capture.messages)} WA messages captured."
    )
