"""
tests/e2e/test_delivery_eta_lifecycle.py — E2E test: delivery ETA full lifecycle.

What this exercises (no Anthropic, no LLM):
  1. Seed restaurant + paid+confirmed delivery order with NO ETA yet.
  2. Admin POST /api/delivery/orders/{id}/eta with minutes=30 → row updated,
     eta_communicated=false (single-winner reset).
  3. Drive scheduler._run_eta_communication() under bypass — it scans, finds
     the order, sends WA (intercepted via wa_capture), and atomically marks
     eta_communicated=true.
  4. Run scheduler again → no second send (idempotency).

Skipped automatically when TEST_DATABASE_URL or ANTHROPIC_API_KEY is unset.
The actual test does NOT call Anthropic.
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
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)


CUSTOMER_PHONE_RAW = "+573009990701"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)


@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """Yields an httpx AsyncClient against the FastAPI app via ASGI transport.

    `wa_capture` injection ensures outbound Meta httpx calls are intercepted
    so the scheduler's send_text doesn't hit the real Graph API.
    """
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_delivery_eta_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    ETA flow:

      Seed: paid + confirmado delivery order, no ETA
      Action 1: admin POST /eta {minutes: 30} → row updated, eta_communicated=false
      Action 2: scheduler tick → finds order, sends WA, marks communicated=true
      Action 3: scheduler tick (again) → no second send (atomic single-winner)
    """
    pool = test_pool

    # The scheduler reads META_ACCESS_TOKEN to send WhatsApp messages.
    # In production it's set; for E2E we set a dummy token — outbound
    # httpx is intercepted by wa_capture so the value never hits Meta.
    monkeypatch.setenv("META_ACCESS_TOKEN", "e2e_dummy_token")

    # ── Seed restaurant ────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E ETA Restaurant",
        bot_number_raw="+570E2EETA",
        num_branches=1,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)

    # ── Seed paid+confirmado delivery order (eligible for ETA) ─────────────────
    order_id = str(uuid.uuid4())
    with bypass_tenant_scope("e2e_eta_setup"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO orders
                     (id, org_id, phone, bot_number, order_type, status,
                      paid, total, subtotal, items)
                   VALUES ($1, $2, $3, $4, 'domicilio', 'confirmado', true,
                           50000, 50000, '[]'::jsonb)""",
                order_id, org_id, CUSTOMER_PHONE, bot_number,
            )

    # ── Admin POST /eta — set ETA to 30 minutes ────────────────────────────────
    owner_email = restaurant["owner_email"]
    admin_token = await create_admin_token(pool, owner_email)
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await e2e_app.post(
        f"/api/delivery/orders/{order_id}/eta",
        json={"minutes": 30},
        headers=auth_headers,
    )
    assert resp.status_code == 200, (
        f"POST /eta failed: {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body.get("estimated_minutes") == 30
    # eta_communicated must reset to false on every set
    assert body.get("eta_communicated") is False, (
        f"Setting ETA must reset communicated flag, got {body!r}"
    )

    # ── Verify in DB ───────────────────────────────────────────────────────────
    with bypass_tenant_scope("e2e_eta_verify_set"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT estimated_minutes, eta_communicated FROM orders WHERE id=$1",
                order_id,
            )
            assert row["estimated_minutes"] == 30
            assert row["eta_communicated"] is False

    # ── Drive the scheduler ETA tick ───────────────────────────────────────────
    # This is the production code path that scans for orders with ETA set
    # but not yet communicated, sends a WA, and marks eta_communicated=true.
    from app.services.scheduler import _run_eta_communication

    # The scheduler runs under bypass at the top level — it scans across orgs
    # and re-enters tenant_scope per org internally. So we don't wrap it here.
    await _run_eta_communication()

    # ── Assert: WA was sent ────────────────────────────────────────────────────
    captured_to_customer = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert captured_to_customer, (
        f"Scheduler did not send any WA to {CUSTOMER_PHONE_RAW}. "
        f"All captured: {wa_capture.all_texts()}"
    )
    # The message must mention the ETA minutes and have the order tag
    eta_msg = " ".join(captured_to_customer)
    assert "30 min" in eta_msg, (
        f"WA message must mention '30 min', got: {eta_msg!r}"
    )
    short_id = order_id[-6:].upper()
    assert short_id in eta_msg, (
        f"WA message must reference order short id {short_id}, got: {eta_msg!r}"
    )

    # ── Assert: eta_communicated flipped to true ──────────────────────────────
    with bypass_tenant_scope("e2e_eta_verify_sent"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT estimated_minutes, eta_communicated FROM orders WHERE id=$1",
                order_id,
            )
            assert row["estimated_minutes"] == 30
            assert row["eta_communicated"] is True, (
                "After scheduler tick, eta_communicated must be TRUE — otherwise "
                "the scheduler would re-send the same ETA on every tick."
            )

    # ── Idempotency: second tick must not send a second WA ─────────────────────
    sends_before = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))
    await _run_eta_communication()
    sends_after = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))
    assert sends_after == sends_before, (
        f"Second scheduler tick re-sent the ETA "
        f"({sends_before} → {sends_after}) — "
        "the eta_communicated flag is not preventing re-sends."
    )

    log.info(
        "e2e.delivery_eta_lifecycle.passed",
        order_id=order_id, minutes=30, sends=sends_after,
    )
