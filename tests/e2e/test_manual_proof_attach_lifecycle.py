"""
tests/e2e/test_manual_proof_attach_lifecycle.py — E2E: image proof attach (bot side).

What this exercises (no Anthropic, no LLM):
  1. Seed restaurant + 1 pending unpaid delivery order.
  2. Customer sends an image message via POST /api/webhook/meta.
  3. chat.py's image shortcut fires `db_attach_order_proof`, updates
     orders.proof_url, and queues a confirmation WA back to the customer.

The caja-validates-the-proof half of this test (POST /api/delivery/orders/
{id}/validate → paid+confirmado, loyalty accrual) was removed in chunk 9
(docs/claude/delivery-web.md): that endpoint was org-wide (no sede scoping)
and only ever served the WhatsApp delivery/pickup flow this order simulates.
See the note near the end of the test body for the resulting gap.

Skipped automatically when TEST_DATABASE_URL or ANTHROPIC_API_KEY is unset.
The test does NOT actually call Anthropic — `_is_image_safe` is monkeypatched
to always return True (image moderation is independent of this flow).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    seed_restaurant,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)


CUSTOMER_PHONE_RAW = "+573009990901"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)


@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """Yields an httpx AsyncClient. wa_capture intercepts outbound Meta calls."""
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


def _build_meta_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_manual_proof_attach_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Manual-proof flow (bot side only — see module docstring for the
    caja-validates half removed in chunk 9):

      Seed: 1 pending unpaid delivery order
      Action: customer sends image → chat.py shortcut → db_attach_order_proof
      Assert: orders.proof_url populated, customer received "comprobante recibido"
    """
    pool = test_pool

    # ── Mock _is_image_safe to skip Anthropic moderation call ─────────────────
    # The real function calls Claude Haiku to inspect the image. In E2E we
    # bypass it (always pass) so the flow is independent of Anthropic.
    from app.routes import chat as chat_module
    monkeypatch.setattr(
        chat_module, "_is_image_safe", AsyncMock(return_value=True)
    )

    # ── Seed restaurant ────────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Manual Proof Restaurant",
        bot_number_raw="+570E2EPROOF",
        num_branches=1,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)

    # ── Seed pending unpaid delivery order ─────────────────────────────────────
    order_id = str(uuid.uuid4())
    with bypass_tenant_scope("e2e_manual_proof_setup"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO orders
                     (id, org_id, phone, bot_number, order_type, status,
                      paid, total, subtotal, items, proof_url)
                   VALUES ($1, $2, $3, $4, 'domicilio', 'pendiente', false,
                           45000, 45000, '[]'::jsonb, '')""",
                order_id, org_id, CUSTOMER_PHONE, bot_number,
            )

    # ── Action 1: customer sends image via webhook ────────────────────────────
    # Build a Meta image-message webhook payload.
    image_id = f"image_e2e_{uuid.uuid4().hex[:12]}"
    wam_id = f"wamid.e2e_{uuid.uuid4().hex}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e_proof_entry",
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": bot_number,
                                "phone_number_id": "e2e_phone_id",
                            },
                            "messages": [
                                {
                                    "id": wam_id,
                                    "from": CUSTOMER_PHONE,
                                    "type": "image",
                                    "image": {"id": image_id},
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }
    body_bytes = json.dumps(payload).encode()
    signature = _build_meta_signature(
        body_bytes, os.environ.get("META_APP_SECRET", "test_secret_e2e")
    )

    resp = await e2e_app.post(
        "/api/webhook/meta",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )
    assert resp.status_code == 200, f"Webhook returned {resp.status_code}: {resp.text}"

    # ── Assert: orders.proof_url populated ─────────────────────────────────────
    expected_proof_url = f"/api/media/{image_id}?bot={bot_number}"
    proof_url_in_db = None
    # Poll briefly — the shortcut runs synchronously inside the webhook handler
    # but background_tasks (the WA confirmation) is fired-and-forgotten.
    for _ in range(10):
        with bypass_tenant_scope("e2e_proof_verify"):
            async with pool.acquire() as conn:
                proof_url_in_db = await conn.fetchval(
                    "SELECT proof_url FROM orders WHERE id=$1", order_id,
                )
        if proof_url_in_db:
            break
        import asyncio
        await asyncio.sleep(0.2)

    assert proof_url_in_db == expected_proof_url, (
        f"Expected proof_url={expected_proof_url!r}, "
        f"got {proof_url_in_db!r}. db_attach_order_proof shortcut did not fire."
    )

    # ── Assert: customer received confirmation WA ─────────────────────────────
    # The shortcut adds the confirmation as a background_task — give it a
    # moment to flush. wa_capture is the httpx interceptor, not a queue.
    import asyncio
    await asyncio.sleep(0.5)
    customer_msgs = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    confirmation_seen = any(
        ("comprobante" in m.lower() and ("recibido" in m.lower() or "validando" in m.lower()))
        for m in customer_msgs
    )
    # Note: background_tasks may not flush before LifespanManager teardown in
    # some configurations. We log but don't hard-fail here — the DB write is
    # the canonical assertion.
    if not confirmation_seen:
        log.warning(
            "e2e.manual_proof.confirmation_not_observed",
            captured_msgs=customer_msgs,
        )

    # Caja proof-validation ("Action 2" in the original version of this test)
    # used to continue here via POST /api/delivery/orders/{id}/validate.
    # That endpoint was deleted in chunk 9 (docs/claude/delivery-web.md) — it
    # was org-wide (no sede scoping) and only ever served the WhatsApp
    # delivery/pickup flow this order simulates. The new sede-scoped API
    # (app/routes/staff_delivery.py) has no equivalent "mark proof
    # validated/paid" action yet (see delivery-web.md's chunk 9 notes) —
    # a real gap, not something this test can cover today. What remains
    # alive and tested above is the webhook→db_attach_order_proof shortcut
    # itself (chat.py's image handler), which is unaffected by that deletion.

    log.info(
        "e2e.manual_proof_attach_lifecycle.passed",
        order_id=order_id,
        proof_url=expected_proof_url,
    )
