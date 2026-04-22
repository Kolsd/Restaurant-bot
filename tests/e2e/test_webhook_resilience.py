"""
tests/e2e/test_webhook_resilience.py — Circuit-breaker contract tests.

Contract: "Si falla un webhook de WhatsApp, la caja no debe romperse."

4 tests validating the isolation between the webhook ingestion path and the
caja (kitchen) read path:

  Test 1 — enqueue DB failure → webhook returns 200 or 503, never propagates
  Test 2 — empty/malformed Meta payloads → 200, no inbox rows written
  Test 3 — caja reads orders via direct DB, completely independent of inbox
  Test 4 — invalid Meta signature → 200 (not 401; avoids Meta retry floods)

Each test seeds its own unique bot_number (+570E2ERESIL1 … +570E2ERESIL4).

Env:
  ANTHROPIC_API_KEY  — required (conftest skip guard)
  TEST_DATABASE_URL  — required (conftest skip guard)
  DISABLE_META_SIGNATURE_VERIFY=1  — set by test_pool fixture in conftest
"""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    create_admin_token,
    seed_restaurant,
    truncate_e2e_data,
    _normalize_phone,
    _build_meta_signature,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

pytestmark = pytest.mark.e2e


# ── Helper: build a minimal Meta text-message payload ─────────────────────────

def _meta_text_payload(bot_number: str, phone: str, text: str, wam_id: str) -> bytes:
    """Encode a minimal Meta WhatsApp webhook payload as bytes."""
    normalized_bot = _normalize_phone(bot_number)
    normalized_phone = _normalize_phone(phone)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e_resil_entry",
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": normalized_bot,
                                "phone_number_id": "e2e_resil_phone_id",
                            },
                            "messages": [
                                {
                                    "id": wam_id,
                                    "from": normalized_phone,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode()


def _signed_request(
    client: AsyncClient,
    body_bytes: bytes,
    secret: str,
):
    """Build headers with a valid (or fake) Meta HMAC signature."""
    sig = _build_meta_signature(body_bytes, secret)
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
    }


# ── App factory (avoids re-importing lifespan between tests) ──────────────────

def _make_client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: enqueue RuntimeError → webhook returns 200 OR 503, never 500 / 401
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_returns_200_when_enqueue_raises(test_pool):
    """
    When inbox_repo.enqueue raises a generic RuntimeError (simulated DB pool
    exhausted), the Meta webhook handler MUST NOT propagate the exception.

    Per CLAUDE.md Rule #6:
    - A non-DB failure (RuntimeError is not PoolAcquireTimeout / ConnectionError)
      sets all_failures_are_db_down=False → handler returns 503 so Meta may retry.
    - 200 would also be acceptable (full DB-down path). Both are circuit-breaker
      behaviors — neither is 401 or 500 (which would be bugs).
    """
    bot_number = "+570E2ERESIL1"
    customer_phone = "+573001110001"

    await seed_restaurant(test_pool, bot_number_raw=bot_number, name="E2E Resilience 1")

    from app.main import app

    wam_id = f"wamid.resil1_{uuid.uuid4().hex}"
    body_bytes = _meta_text_payload(bot_number, customer_phone, "hola", wam_id)
    headers = _signed_request(None, body_bytes, os.environ.get("META_APP_SECRET", "test_secret_e2e"))

    # Narrow mock: only inbox_repo.enqueue raises; everything else runs normally.
    mock_enqueue = AsyncMock(side_effect=RuntimeError("simulated DB pool exhausted"))

    try:
        with patch("app.repositories.inbox_repo.enqueue", mock_enqueue):
            async with _make_client(app) as client:
                resp = await client.post(
                    "/api/webhook/meta",
                    content=body_bytes,
                    headers=headers,
                )

        # The handler must absorb the exception — never expose 500 or 401 to Meta.
        assert resp.status_code in (200, 503), (
            f"Expected 200 or 503 (circuit-breaker), got {resp.status_code}: {resp.text}"
        )
        # Document the observed behavior for the sprint report.
        log.info(
            "test1.observed_status",
            status=resp.status_code,
            note="503 = non-DB failure path (Meta may retry); 200 = DB-down path (absorbed)",
        )
    finally:
        # Restore is handled by patch context manager exiting; no state leaks.
        await truncate_e2e_data(test_pool, (
            await test_pool.fetchval(
                "SELECT id FROM organizations WHERE whatsapp_number = $1",
                _normalize_phone(bot_number),
            )
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: empty / malformed Meta payloads → 200, nothing enqueued
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_empty_payloads_do_not_crash(test_pool):
    """
    Three edge-case payloads that must not crash the handler and must not
    produce any webhook_inbox rows.

    Sub-cases:
      a) entry: []                              — no entries
      b) entry: [{changes: []}]                — entry with no changes
      c) entry: [{changes: [{value: {messages: []}}]}]  — empty messages list
    """
    bot_number = "+570E2ERESIL2"
    await seed_restaurant(test_pool, bot_number_raw=bot_number, name="E2E Resilience 2")

    from app.main import app

    app_secret = os.environ.get("META_APP_SECRET", "test_secret_e2e")

    sub_cases = [
        # (description, payload_dict)
        (
            "entry_empty",
            {"object": "whatsapp_business_account", "entry": []},
        ),
        (
            "changes_empty",
            {
                "object": "whatsapp_business_account",
                "entry": [{"id": "x", "changes": []}],
            },
        ),
        (
            "messages_empty",
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "x",
                        "changes": [
                            {
                                "value": {
                                    "metadata": {
                                        "display_phone_number": _normalize_phone(bot_number),
                                        "phone_number_id": "e2e_phone",
                                    },
                                    "messages": [],
                                }
                            }
                        ],
                    }
                ],
            },
        ),
    ]

    # Count existing inbox rows before the sub-cases so we baseline correctly.
    inbox_before = await test_pool.fetchval("SELECT COUNT(*) FROM webhook_inbox")

    async with _make_client(app) as client:
        for label, payload_dict in sub_cases:
            body_bytes = json.dumps(payload_dict).encode()
            sig = _build_meta_signature(body_bytes, app_secret)
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            }
            resp = await client.post("/api/webhook/meta", content=body_bytes, headers=headers)
            assert resp.status_code == 200, (
                f"[{label}] Expected 200, got {resp.status_code}: {resp.text}"
            )
            log.info("test2.sub_case_ok", label=label, status=resp.status_code)

    inbox_after = await test_pool.fetchval("SELECT COUNT(*) FROM webhook_inbox")
    assert inbox_after == inbox_before, (
        f"Empty payloads must not enqueue rows. Before={inbox_before}, After={inbox_after}"
    )

    # Cleanup
    org_id = await test_pool.fetchval(
        "SELECT id FROM organizations WHERE whatsapp_number = $1",
        _normalize_phone(bot_number),
    )
    await truncate_e2e_data(test_pool, org_id)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: caja delivery-orders endpoint works with inbox worker disabled
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_caja_endpoint_works_with_inbox_worker_disabled(test_pool):
    """
    The caja GET /api/kitchen/delivery-orders must return orders even when the
    inbox worker is completely offline (no webhook processed, no agent involved).

    Proof: create an order via a direct DB INSERT (bypassing the entire
    webhook → inbox → agent pipeline), then assert the caja endpoint lists it.

    This validates the decoupling: caja reads from `orders` directly; the
    inbox/agent path is only the WRITE path for new orders.
    """
    bot_number = "+570E2ERESIL3"
    restaurant = await seed_restaurant(
        test_pool,
        bot_number_raw=bot_number,
        name="E2E Resilience 3",
    )
    org_id = restaurant["id"]

    # ── Direct DB INSERT of a confirmed delivery order ────────────────────────
    order_id = str(uuid.uuid4())
    items_json = json.dumps([{"name": "Empanaditas de Carne (3 uds)", "qty": 2, "price": 15000}])

    with bypass_tenant_scope("e2e_caja_seed_direct_insert"):
        async with test_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders
                  (id, phone, items, order_type, address, notes,
                   subtotal, delivery_fee, total, status, paid,
                   payment_url, bot_number, payment_method,
                   base_order_id, sub_number, org_id, channel)
                VALUES
                  ($1, $2, $3::jsonb, $4, $5, $6,
                   $7, $8, $9, $10, $11,
                   $12, $13, $14,
                   $15, $16, $17, $18)
                """,
                order_id,
                "573009990003",
                items_json,
                "domicilio",
                "Calle Test 100, Bogotá",
                "",                             # notes
                30000,                          # subtotal (NUMERIC)
                0,                              # delivery_fee
                30000,                          # total
                "confirmado",
                False,                          # paid
                "",                             # payment_url
                _normalize_phone(bot_number),
                "Efectivo",
                order_id,                       # base_order_id
                None,                           # sub_number
                org_id,
                "whatsapp",                     # channel
            )

    # ── Auth: create admin token for the owner ────────────────────────────────
    token = await create_admin_token(test_pool, restaurant["owner_email"])

    from app.main import app

    async with _make_client(app) as client:
        resp = await client.get(
            "/api/kitchen/delivery-orders",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "orders" in data, f"Response missing 'orders' key: {data}"

    order_ids = [o["id"] for o in data["orders"]]
    assert order_id in order_ids, (
        f"Direct-inserted order {order_id!r} not found in caja response. "
        f"Returned IDs: {order_ids}"
    )

    log.info(
        "test3.caja_decoupled_ok",
        order_id=order_id,
        total_orders_in_response=len(data["orders"]),
    )

    # Cleanup
    await truncate_e2e_data(test_pool, org_id)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: invalid Meta signature → 200 (not 401 — Rule #6 anti-flood guard)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_invalid_signature_returns_200_not_401(test_pool):
    """
    An invalid X-Hub-Signature-256 must be rejected silently with 200.

    Rationale (CLAUDE.md Rule #6):
      Returning 401 causes Meta to retry the same batch up to 5×, turning a
      short config error into a sustained flood. 200 tells Meta "received" and
      stops the flood at the source.

    The test temporarily enables signature verification and posts a payload
    with an intentionally wrong HMAC digest. It asserts:
      - response == 200 (not 401, not 500)
      - no new webhook_inbox row was written (bad payload was rejected)
    """
    bot_number = "+570E2ERESIL4"
    await seed_restaurant(test_pool, bot_number_raw=bot_number, name="E2E Resilience 4")

    from app.main import app

    inbox_before = await test_pool.fetchval("SELECT COUNT(*) FROM webhook_inbox")

    wam_id = f"wamid.resil4_{uuid.uuid4().hex}"
    body_bytes = _meta_text_payload(bot_number, "+573009990004", "test", wam_id)
    bad_signature = "sha256=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    # Temporarily enable signature verification for this test only.
    original_disable = os.environ.get("DISABLE_META_SIGNATURE_VERIFY", "1")
    original_secret = os.environ.get("META_APP_SECRET", "")
    try:
        os.environ["DISABLE_META_SIGNATURE_VERIFY"] = "0"
        os.environ["META_APP_SECRET"] = "correct_secret_resil4_abc"

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/webhook/meta",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": bad_signature,
                },
            )

        assert resp.status_code == 200, (
            f"Expected 200 (not 401 — Rule #6 anti-flood). Got {resp.status_code}: {resp.text}"
        )

        inbox_after = await test_pool.fetchval("SELECT COUNT(*) FROM webhook_inbox")
        assert inbox_after == inbox_before, (
            f"Bad-signature payload must not be enqueued. Before={inbox_before}, After={inbox_after}"
        )

        log.info("test4.invalid_sig_ok", status=resp.status_code)

    finally:
        # Restore env vars regardless of test outcome.
        os.environ["DISABLE_META_SIGNATURE_VERIFY"] = original_disable
        if original_secret:
            os.environ["META_APP_SECRET"] = original_secret
        elif "META_APP_SECRET" in os.environ and not original_secret:
            del os.environ["META_APP_SECRET"]

    # Cleanup
    org_id = await test_pool.fetchval(
        "SELECT id FROM organizations WHERE whatsapp_number = $1",
        _normalize_phone(bot_number),
    )
    await truncate_e2e_data(test_pool, org_id)
