"""
tests/e2e/test_delivery_nequi_proof_lifecycle.py — E2E test: Nequi comprobante
(payment proof) flow on delivery orders.

Architecture notes (discovered during exploration):

1. PROOF COLUMN IN ORDERS: There is NO `proof_media_url` or `comprobante` column in
   the `orders` table. Delivery proof is handled conversationally:
   - The order commits at STEP 6 (after customer confirmation), BEFORE proof.
   - The bot then asks for a payment screenshot (STEP 6b) in the same reply.
   - When the customer sends an image, chat.py converts it to:
       user_text = "📸 [IMAGEN RECIBIDA] Link del comprobante: /api/media/{id}?bot={num}"
   - This text is fed to the LLM and persisted in conversations.history (JSONB).
   - The `proof_media_url` column only exists in `table_checks` (salon flow).

2. REQUIRES_PROOF STATE: That state key lives in agent_salon.py checkout state machine
   (for dine-in/salon). agent_external.py (delivery) has no such gate — it creates the
   order first, then requests proof as a follow-up.

3. /api/media/{media_id}: Requires Bearer token (get_current_user). No auth → 422/401/403.
   The endpoint only downloads from Meta — it does NOT persist bytes to the DB.

4. IMAGE WEBHOOK PAYLOAD: chat.py handles `msg_type == "image"` at lines 419-456.
   The "shortcut" path (db_attach_proof) only fires when an open salon proposal
   with status "awaiting_proof" exists. For delivery it falls through to the LLM path.

5. THE ADVERSARIAL REFRAME: The instructions say "confirm without proof → order should not
   exist yet". This does NOT hold for external (delivery) orders: the order commits at the
   confirmation turn (STEP 6), BEFORE the image is sent. The correct adversarial is:
   (a) If the customer skips proof and sends a new text, the bot acknowledges the
       comprobante reminder — the order is already in status "pendiente" (which is correct),
       NOT "confirmado" by caja yet. Admin can only confirm after manually validating.
   (b) /api/media/ without auth → 4xx.

   Rule #3 (Checkout State Machine — requires_proof) governs salon flow only. The task
   instruction "order should not exist" if no proof is sent refers to the salon path.
   For delivery, we guard instead: orders.paid = False and orders.status = "pendiente"
   until admin manually confirms.

Prerequisites:
  ANTHROPIC_API_KEY   -- real Anthropic key
  TEST_DATABASE_URL   -- Postgres URL pointing at isolated test DB
  DISABLE_EMBEDDED_WORKER=1  -- set automatically by conftest.py

Run:
  pytest tests/e2e/test_delivery_nequi_proof_lifecycle.py -v -s -m e2e
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
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

CUSTOMER_PHONE_RAW = "+573007776655"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

CUSTOMER_LAT = 4.711200
CUSTOMER_LON = -74.072300
CUSTOMER_ADDRESS = "Calle 85 # 11-25, apto 302, Bogota"

MENU = {
    "Bowls": [
        {
            "name": "Bowl de Pollo",
            "description": "Bowl de arroz, pollo a la plancha y aguacate",
            "price": 22000,
            "active": True,
        }
    ]
}

# Include Nequi + Efectivo so the bot offers Nequi as a valid option
PAYMENT_METHODS = ["Nequi", "Efectivo"]


# ── Fake media_id for the comprobante image ────────────────────────────────────

def _fake_media_id() -> str:
    return f"wamid.e2e_proof_{uuid.uuid4().hex[:12]}"


# ── Local helper: build an image-type WhatsApp inbound payload ─────────────────

async def simulate_whatsapp_image_inbound(
    client: AsyncClient,
    pool: asyncpg.Pool,
    *,
    phone: str,
    image_id: str,
    bot_number: str,
    wam_id: str | None = None,
) -> int:
    """
    Send a simulated WhatsApp image message to POST /webhook/meta.

    Builds the correct `type=image` payload shape that chat.py expects at line 419:
      message.get("image", {}).get("id", "")

    Returns number of inbox items processed.

    We do NOT extend the shared conftest to avoid regressing other tests.
    The payload shape matches what Meta sends for image messages.

    Note on _is_image_safe:
      chat.py calls _is_image_safe(image_id, access_token) for salon proposals
      awaiting_proof. For delivery orders (no open proposal), that shortcut branch
      is not entered and _is_image_safe is NOT called. The image falls straight to:
        user_text = "📸 [IMAGEN RECIBIDA] Link del comprobante: /api/media/{id}?bot={num}"
      which is enqueued and dispatched to the LLM. No additional mock needed.
    """
    import os
    import hashlib
    import hmac as _hmac

    if wam_id is None:
        wam_id = f"wamid.e2e_{uuid.uuid4().hex}"

    normalized_bot = _normalize_phone(bot_number)

    message = {
        "id": wam_id,
        "from": _normalize_phone(phone),
        "type": "image",
        "image": {
            "id": image_id,
            "mime_type": "image/jpeg",
            "sha256": "e2e_fake_sha256",
        },
    }

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e_nequi_entry",
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": normalized_bot,
                                "phone_number_id": "e2e_nequi_phone_id",
                            },
                            "messages": [message],
                        }
                    }
                ],
            }
        ],
    }

    body_bytes = json.dumps(payload).encode()
    app_secret = os.environ.get("META_APP_SECRET", "test_secret_e2e")

    def _build_sig(body: bytes, secret: str) -> str:
        return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    signature = _build_sig(body_bytes, app_secret)

    resp = await client.post(
        "/api/webhook/meta",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )
    assert resp.status_code == 200, f"Image webhook returned {resp.status_code}: {resp.text}"

    processed = await drain_inbox(pool)
    log.info(
        "simulate_whatsapp_image_inbound.done",
        phone=phone,
        image_id=image_id,
        inbox_processed=processed,
    )
    return processed


# ── App fixture ────────────────────────────────────────────────────────────────

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


# ── Happy path: Nequi proof lifecycle ─────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delivery_nequi_proof_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Full Nequi delivery lifecycle:

    Turn 1: "Hola, quiero pedir a domicilio un Bowl de Pollo"
    Turn 2: text address
    Turn 2b (optional): GPS location if bot re-asks
    Turn 3: "Nequi"         → bot creates order + asks for comprobante
    Turn 4: image message    → bot acknowledges receipt
    Turn 5: admin confirm    → PATCH status → "confirmado"

    Assertions:
    - orders.order_type == "domicilio"
    - orders.status == "pendiente" (after bot turn, before admin confirm)
    - orders.payment_method contains "nequi" (case-insensitive)
    - orders.paid is False (Nequi is manual validation, not Wompi auto-paid)
    - orders.org_id == parent_id
    - conversations.history contains "📸 [IMAGEN RECIBIDA]" entry (proof recorded)
    - After admin PATCH: orders.status == "confirmado" (exact)
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Nequi Proof Restaurant",
        bot_number_raw="+570E2ENEQUI01",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),
            (4.609710, -74.081741),
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, parent_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    # Reset in-process fallback state
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
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    log.info(
        "e2e.nequi.test_start",
        parent_id=parent_id,
        bot_number=bot_number,
        customer_phone=CUSTOMER_PHONE,
    )

    # ── Turn 1: greeting + item + delivery type ───────────────────────────────
    turn1_text = "Hola, quiero pedir a domicilio un Bowl de Pollo"
    log.info("e2e.nequi.turn_1", text=turn1_text)
    t1_start = time.monotonic()
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=turn1_text,
        bot_number=bot_number,
    )
    log.info("e2e.nequi.turn_1_done", processed=processed_1, elapsed_s=round(time.monotonic() - t1_start, 1))
    assert processed_1 >= 1, "Turn 1: inbox item was not processed"

    turn1_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(turn1_replies) >= 1, (
        "Turn 1: bot sent no WA reply. Check ANTHROPIC_API_KEY and bot_number lookup."
    )

    # ── Turn 2: text address ──────────────────────────────────────────────────
    turn2_text = f"Mi dirección es {CUSTOMER_ADDRESS}"
    log.info("e2e.nequi.turn_2", text=turn2_text)
    t2_start = time.monotonic()
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=turn2_text,
        bot_number=bot_number,
    )
    log.info("e2e.nequi.turn_2_done", processed=processed_2, elapsed_s=round(time.monotonic() - t2_start, 1))
    assert processed_2 >= 1, "Turn 2: address inbox item was not processed"

    turn2_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert len(turn2_replies) >= 2, "Turn 2: bot sent no reply after address"

    # ── Turn 2b (optional): GPS location ─────────────────────────────────────
    latest_reply_2 = turn2_replies[-1].lower() if turn2_replies else ""
    needs_gps = any(
        kw in latest_reply_2
        for kw in ("ubicaci", "gps", "localizaci", "direcci", "mapa")
    )
    if needs_gps:
        log.info("e2e.nequi.turn_2b_gps")
        t2b_start = time.monotonic()
        processed_2b = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW,
            text="",
            bot_number=bot_number,
            lat=CUSTOMER_LAT,
            lon=CUSTOMER_LON,
        )
        log.info("e2e.nequi.turn_2b_done", processed=processed_2b, elapsed_s=round(time.monotonic() - t2b_start, 1))
        assert processed_2b >= 1, "Turn 2b: GPS inbox item was not processed"
        turn2b_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
        assert len(turn2b_replies) >= 3, "Turn 2b: bot sent no reply after GPS"

    # ── Turn 3: payment method = Nequi ───────────────────────────────────────
    # Per agent_external.py STEP 4 → 5 → 6 funnel:
    # Turn 3 provides payment method. Bot then asks for confirmation (STEP 5).
    # We also handle the case where the bot bundles confirmation ask and we
    # do a 2-step: payment then confirm.
    turn3_text = "Nequi"
    log.info("e2e.nequi.turn_3", text=turn3_text)
    t3_start = time.monotonic()
    processed_3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=turn3_text,
        bot_number=bot_number,
    )
    log.info("e2e.nequi.turn_3_done", processed=processed_3, elapsed_s=round(time.monotonic() - t3_start, 1))
    assert processed_3 >= 1, "Turn 3: payment method inbox item was not processed"

    turn3_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.nequi.turn_3_replies", replies=[r[:80] for r in turn3_replies])

    # ── Turn 4: confirmation (STEP 5 → STEP 6 — order creation) ──────────────
    # After Nequi selection, the bot summarizes and asks for confirmation.
    # "sí confirmo" → agent creates the order (create_delivery_order tool).
    turn4_text = "sí confirmo"
    log.info("e2e.nequi.turn_4_confirm", text=turn4_text)
    t4_start = time.monotonic()
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=turn4_text,
        bot_number=bot_number,
    )
    log.info("e2e.nequi.turn_4_done", processed=processed_4, elapsed_s=round(time.monotonic() - t4_start, 1))
    assert processed_4 >= 1, "Turn 4: confirmation inbox item was not processed"

    turn4_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.nequi.turn_4_replies", replies=[r[:100] for r in turn4_replies[-3:]])

    # The bot should have mentioned "comprobante" (📸) after creating the Nequi order.
    # Per agent_external.py STEP 6b: "Para completar tu pedido, por favor envíanos
    # el comprobante de pago (foto o captura) 📸"
    all_bot_text_so_far = " ".join(turn4_replies)
    bot_requested_proof = re.search(r"comprobante|📸|foto|captura", all_bot_text_so_far, re.IGNORECASE)
    log.info(
        "e2e.nequi.proof_request_check",
        bot_requested_proof=bool(bot_requested_proof),
        all_text_preview=all_bot_text_so_far[-200:],
    )

    # ── Assert: order in DB (created after STEP 6 confirmation) ──────────────
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_nequi_assert_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, phone, order_type, status, paid, total,
                           org_id, payment_method
                    FROM orders
                    WHERE phone = $1
                      AND order_type = 'domicilio'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    assert order_row is not None, (
        f"No delivery order found for phone {CUSTOMER_PHONE} in DB after Nequi confirmation. "
        "Expected the agent to call create_delivery_order tool at STEP 6. "
        "Check: (1) ANTHROPIC_API_KEY is valid, (2) bot resolved to seeded restaurant, "
        "(3) agent did not error before calling the tool."
    )

    order_id = order_row["id"]
    log.info(
        "e2e.nequi.order_found",
        order_id=order_id,
        order_type=order_row["order_type"],
        status=order_row["status"],
        paid=order_row["paid"],
        payment_method=order_row["payment_method"],
        total=order_row["total"],
        org_id=order_row["org_id"],
    )

    # order_type must be domicilio
    assert order_row["order_type"] == "domicilio", (
        f"Expected order_type='domicilio', got '{order_row['order_type']}'"
    )

    # status must be pendiente (not auto-confirmed — Nequi requires manual caja validation)
    assert order_row["status"] == "pendiente", (
        f"Expected status='pendiente', got '{order_row['status']}'. "
        "Nequi orders should remain pendiente until the caja manually validates the proof."
    )

    # paid must be False (Nequi is not Wompi-auto-paid)
    assert order_row["paid"] is False, (
        f"Expected paid=False for Nequi delivery order, got paid={order_row['paid']}. "
        "Nequi proof requires manual caja validation — auto-paid=True is a bug."
    )

    # payment_method must contain "nequi" (case-insensitive)
    pm = (order_row["payment_method"] or "").lower()
    assert "nequi" in pm, (
        f"Expected payment_method to contain 'nequi', got '{order_row['payment_method']}'. "
        "The agent should have passed payment_method='Nequi' to create_delivery_order."
    )

    # org_id must match parent_id (tenant key, post-Wave-2)
    assert order_row["org_id"] == parent_id, (
        f"Order org_id={order_row['org_id']} does not match parent_id={parent_id}. "
        "RLS tenant isolation failure — order written under wrong org."
    )

    # total >= 22000 (item price)
    from app.services.money import to_decimal
    order_total = to_decimal(order_row["total"])
    assert order_total >= Decimal("22000"), (
        f"Expected total >= 22000 (Bowl de Pollo price), got {order_total}"
    )

    # ── Turn 5: customer sends comprobante image ───────────────────────────────
    # Build a fake media_id. The webhook enqueues the image as:
    #   user_text = "📸 [IMAGEN RECIBIDA] Link del comprobante: /api/media/{id}?bot={num}"
    # This is stored in conversations.history and the LLM acknowledges it.
    # No Mock needed: chat.py only calls _is_image_safe + db_attach_proof
    # when there's a salon proposal in status="awaiting_proof". For delivery
    # orders there's no such proposal → falls straight to LLM path.
    proof_media_id = _fake_media_id()
    log.info("e2e.nequi.turn_5_image", media_id=proof_media_id)
    t5_start = time.monotonic()
    processed_5 = await simulate_whatsapp_image_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        image_id=proof_media_id,
        bot_number=bot_number,
    )
    log.info("e2e.nequi.turn_5_done", processed=processed_5, elapsed_s=round(time.monotonic() - t5_start, 1))
    assert processed_5 >= 1, "Turn 5: image inbox item was not processed"

    turn5_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    log.info("e2e.nequi.turn_5_replies", replies=[r[:100] for r in turn5_replies[-3:]])

    # ── Assert: conversation history records the proof image ──────────────────
    # The image turn was enqueued as user_text containing "/api/media/{id}".
    # After LLM dispatch, conversations.history JSONB should contain that string.
    conv_row = None
    for attempt in range(10):
        with bypass_tenant_scope("e2e_nequi_assert_conversation"):
            async with pool.acquire() as conn:
                conv_row = await conn.fetchrow(
                    """
                    SELECT history
                    FROM conversations
                    WHERE phone = $1 AND bot_number = $2
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                    bot_number,
                )
        if conv_row:
            break
        await asyncio.sleep(0.3)

    assert conv_row is not None, (
        f"No conversation record found for phone={CUSTOMER_PHONE} bot={bot_number}. "
        "The inbox worker must persist history in conversations table."
    )

    history = conv_row["history"]
    if isinstance(history, str):
        history = json.loads(history)

    # Find any message in history that contains the media_id reference
    # (stored as "/api/media/{proof_media_id}" in the user turn content)
    proof_in_history = any(
        isinstance(m.get("content"), str) and f"/api/media/{proof_media_id}" in m["content"]
        for m in history
    )
    log.info(
        "e2e.nequi.proof_in_history",
        proof_in_history=proof_in_history,
        history_length=len(history),
        last_user_content=[
            m.get("content", "")[:80]
            for m in history
            if m.get("role") == "user"
        ][-3:],
    )
    assert proof_in_history, (
        f"Expected conversation history to contain '/api/media/{proof_media_id}' in a user turn. "
        f"Conversation has {len(history)} messages. "
        "This means the image webhook payload did not route correctly to user_text — "
        "check chat.py image branch at msg_type=='image'."
    )

    # ── Assert: order still pendiente and unpaid after proof image ────────────
    # The proof is conversational — the order status does NOT auto-update to
    # "confirmado" when the customer sends the image. Caja must do it manually.
    with bypass_tenant_scope("e2e_nequi_assert_still_pendiente"):
        async with pool.acquire() as conn:
            status_row = await conn.fetchrow(
                "SELECT status, paid FROM orders WHERE id = $1",
                order_id,
            )

    assert status_row is not None, f"Order {order_id} disappeared from DB"
    assert status_row["status"] == "pendiente", (
        f"Expected order to remain 'pendiente' after proof image. "
        f"Got '{status_row['status']}'. "
        "The bot should NOT auto-confirm Nequi — caja must validate."
    )
    assert status_row["paid"] is False, (
        f"Order marked paid=True after image turn — should remain False until caja confirms."
    )

    # ── Admin (caja): PATCH pendiente → confirmado ────────────────────────────
    log.info("e2e.nequi.patch_confirmado", order_id=order_id)
    patch_resp = await client.patch(
        f"/api/delivery/orders/{order_id}/status",
        json={"status": "confirmado"},
        headers=auth_headers,
    )
    assert patch_resp.status_code == 200, (
        f"Admin PATCH to confirmado failed: {patch_resp.status_code} {patch_resp.text}. "
        "This endpoint is tested in test_delivery_lifecycle.py — check orders_routes.py."
    )

    # ── Assert: final status = confirmado (exact, no OR-chains) ───────────────
    with bypass_tenant_scope("e2e_nequi_assert_confirmado"):
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                "SELECT status, paid FROM orders WHERE id = $1",
                order_id,
            )

    assert final_row is not None, f"Order {order_id} disappeared after admin confirm"
    assert final_row["status"] == "confirmado", (
        f"Expected final status='confirmado' after admin PATCH, got '{final_row['status']}'"
    )

    # paid still False — Nequi confirmation is a caja acknowledgement, not Wompi
    assert final_row["paid"] is False, (
        f"Expected paid=False even after caja confirms (Nequi is manual, not Wompi auto-paid). "
        f"Got paid={final_row['paid']}"
    )

    log.info(
        "e2e.nequi.test_passed",
        order_id=order_id,
        payment_method=order_row["payment_method"],
        proof_media_id=proof_media_id,
        proof_in_history=proof_in_history,
        final_status=final_row["status"],
    )
    print(
        f"\n[OK] E2E Nequi proof test passed: "
        f"order {order_id}, payment_method='{order_row['payment_method']}', "
        f"proof in history={proof_in_history}, final_status='{final_row['status']}'"
    )


# ── Adversarial 1: no proof sent → order already exists at pendiente ──────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delivery_nequi_no_proof_order_stays_pendiente(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Adversarial 1: The customer completes the Nequi delivery flow (address → Nequi →
    confirm) but does NOT send a proof image. The order must exist (delivery orders
    commit at STEP 6) but must remain status='pendiente', paid=False.

    Design note: Unlike the salon `requires_proof` gate (which blocks a caja action),
    there is no server-side gate preventing the bot from receiving the order. The
    safety guarantee for Nequi is:
      - orders.paid = False (never auto-paid)
      - orders.status = "pendiente" (admin must manually confirm after seeing proof)

    If the bot accidentally sets paid=True or auto-advances to "confirmado" without
    an image — THAT is the regression. This test guards against it.
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E Nequi No-Proof Restaurant",
        bot_number_raw="+570E2ENEQUI02",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=2,
        branch_latlons=[
            (4.710989, -74.072092),
            (4.609710, -74.081741),
        ],
    )
    parent_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, parent_id)
    for branch in restaurant["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    log.info(
        "e2e.nequi.noproof.test_start",
        parent_id=parent_id,
        bot_number=bot_number,
    )

    # Turn 1
    processed_1 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Hola, quiero pedir a domicilio un Bowl de Pollo",
        bot_number=bot_number,
    )
    assert processed_1 >= 1, "Turn 1 not processed"

    # Turn 2: address
    processed_2 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text=f"Mi dirección es {CUSTOMER_ADDRESS}",
        bot_number=bot_number,
    )
    assert processed_2 >= 1, "Turn 2 not processed"

    # Turn 2b: GPS (conditional)
    t2_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    latest = t2_replies[-1].lower() if t2_replies else ""
    if any(kw in latest for kw in ("ubicaci", "gps", "localizaci", "direcci", "mapa")):
        processed_2b = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW,
            text="",
            bot_number=bot_number,
            lat=CUSTOMER_LAT,
            lon=CUSTOMER_LON,
        )
        assert processed_2b >= 1, "Turn 2b GPS not processed"

    # Turn 3: payment method
    processed_3 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="Nequi",
        bot_number=bot_number,
    )
    assert processed_3 >= 1, "Turn 3 not processed"

    # Turn 4: confirmation (creates the order, then bot asks for comprobante)
    processed_4 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="sí confirmo",
        bot_number=bot_number,
    )
    assert processed_4 >= 1, "Turn 4 not processed"

    # ── Customer does NOT send a comprobante image ─────────────────────────────
    # Instead they send an unrelated text (or nothing). We send "gracias" which
    # the bot treats as an acknowledgement (POST-COMPROBANTE RULES in agent_external.py).
    processed_5 = await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW,
        text="gracias",
        bot_number=bot_number,
    )
    assert processed_5 >= 1, "Turn 5 (gracias without proof) not processed"

    # ── Assert: order exists but remains pendiente + unpaid ───────────────────
    order_row = None
    for attempt in range(20):
        with bypass_tenant_scope("e2e_nequi_noproof_assert"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT id, status, paid, payment_method
                    FROM orders
                    WHERE phone = $1
                      AND order_type = 'domicilio'
                      AND status NOT IN ('cancelado')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    CUSTOMER_PHONE,
                )
        if order_row:
            break
        await asyncio.sleep(0.5)

    # For Nequi delivery, order is created at confirmation (STEP 6), before proof.
    # Unlike the salon `requires_proof` gate, the delivery flow commits the order
    # immediately. The test verifies it remains pendiente+unpaid without proof.
    assert order_row is not None, (
        f"No order found for phone {CUSTOMER_PHONE} even after confirmation. "
        "Check that the bot called create_delivery_order."
    )

    pm = (order_row["payment_method"] or "").lower()
    assert "nequi" in pm, (
        f"payment_method should be 'nequi', got '{order_row['payment_method']}'"
    )

    assert order_row["status"] == "pendiente", (
        f"Order status should remain 'pendiente' without proof. "
        f"Got '{order_row['status']}'. "
        "A bot turn of 'gracias' MUST NOT auto-advance the order status."
    )

    assert order_row["paid"] is False, (
        f"Order paid should remain False without a comprobante. "
        f"Got paid={order_row['paid']}."
    )

    log.info(
        "e2e.nequi.noproof.test_passed",
        order_id=order_row["id"],
        status=order_row["status"],
        paid=order_row["paid"],
    )
    print(
        f"\n[OK] Adversarial 1 passed: Nequi order exists but stays pendiente+unpaid "
        f"when no proof is sent. order_id={order_row['id']}, status='pendiente', paid=False"
    )


# ── Adversarial 2: /api/media/{id} requires auth ──────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_media_proxy_requires_auth(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Adversarial 2: GET /api/media/{media_id} without a Bearer token must return
    4xx (401, 403, or 422). A 200 response would mean unauthenticated access to
    customer payment proofs — a cross-tenant data leak.

    Per chat.py line 94: the endpoint depends on get_current_user which checks
    the Authorization header. No token → FastAPI DI raises 401/403/422.
    """
    client = e2e_app

    fake_media_id = _fake_media_id()
    log.info("e2e.nequi.media_proxy_auth_check", media_id=fake_media_id)

    # ── No auth at all ────────────────────────────────────────────────────────
    resp_no_auth = await client.get(f"/api/media/{fake_media_id}?bot=570E2ENEQUI01")
    log.info(
        "e2e.nequi.media_no_auth",
        status_code=resp_no_auth.status_code,
        body_preview=resp_no_auth.text[:100],
    )
    assert resp_no_auth.status_code in (401, 403, 422), (
        f"GET /api/media/ without auth returned {resp_no_auth.status_code}. "
        "Expected 401, 403, or 422 (FastAPI DI rejection). "
        "A 200 here means unauthenticated download of customer payment proofs — security gap."
    )

    # ── Malformed / garbage token ──────────────────────────────────────────────
    resp_bad_token = await client.get(
        f"/api/media/{fake_media_id}?bot=570E2ENEQUI01",
        headers={"Authorization": "Bearer definitely_not_a_valid_jwt_or_session_token"},
    )
    log.info(
        "e2e.nequi.media_bad_token",
        status_code=resp_bad_token.status_code,
        body_preview=resp_bad_token.text[:100],
    )
    assert resp_bad_token.status_code in (401, 403, 422), (
        f"GET /api/media/ with garbage token returned {resp_bad_token.status_code}. "
        "Expected 401, 403, or 422. "
        "Invalid tokens must be rejected — not granted access to media downloads."
    )

    log.info(
        "e2e.nequi.media_proxy_auth.test_passed",
        no_auth_status=resp_no_auth.status_code,
        bad_token_status=resp_bad_token.status_code,
    )
    print(
        f"\n[OK] Adversarial 2 passed: "
        f"/api/media/ without auth returned {resp_no_auth.status_code}, "
        f"with garbage token returned {resp_bad_token.status_code}. "
        "Unauthorized download of customer proof images is correctly blocked."
    )
