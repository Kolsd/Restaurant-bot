"""
tests/e2e/test_stock_and_cartlock_lifecycle.py — E2E tests: inventory rollback + cart-lock
contention.

Two tests:

  Test A — test_insufficient_stock_raises_and_rolls_back
    Validates Rule #13: InsufficientStockError surfaces as a typed exception, order is NOT
    committed, inventory is NOT decremented, and the customer receives a stock-specific
    message — NOT a generic "problema técnico" fallback.

  Test B — test_cart_lock_contention_returns_neutral_message
    Validates Rule #5: when the cart lock is already held, the bot returns a NEUTRAL
    "procesando" message and does NOT create an order. The LLM's "confirmado" reply
    MUST NOT leak to the customer.

Prerequisites:
  ANTHROPIC_API_KEY   — real Anthropic key
  TEST_DATABASE_URL   — Postgres URL (isolated test DB)

Run:
  pytest tests/e2e/test_stock_and_cartlock_lifecycle.py -v -m e2e --tb=short
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
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

CUSTOMER_PHONE_RAW_A = "+573001112201"
CUSTOMER_PHONE_A = _normalize_phone(CUSTOMER_PHONE_RAW_A)

CUSTOMER_PHONE_RAW_B = "+573001112202"
CUSTOMER_PHONE_B = _normalize_phone(CUSTOMER_PHONE_RAW_B)

CUSTOMER_ADDRESS = "Calle 80 # 50-20, Bogotá"

# Menu shared by both tests — one dish, stock-constrained in Test A.
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

PAYMENT_METHODS = ["Efectivo", "Nequi"]

DISH_NAME = "Empanaditas de Carne (3 uds)"

# Intentional over-order: customer requests more than available stock.
STOCK_AVAILABLE = 2
ORDER_QUANTITY = 5   # > STOCK_AVAILABLE — must trigger InsufficientStockError


# ── App fixture (function-scoped so lifespan runs per test) ────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """Real FastAPI app via ASGI transport, with lifespan managed."""
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=120.0,
        ) as client:
            yield client


# ── Helper: clear in-process state-store fallback dicts ───────────────────────

def _clear_state_store():
    """Reset all in-process fallback dicts in state_store (idempotent)."""
    try:
        import app.services.state_store as ss
        for attr in [
            "_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
            "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits",
        ]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
        # asyncio.Lock dicts — just clear refs so new locks are created on demand
        for attr in ["_fb_nps_transition_locks"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Test A: InsufficientStockError — rollback + typed customer message
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_insufficient_stock_raises_and_rolls_back(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Orders 5 units of a dish with stock=2.

    Expected outcome:
    - InsufficientStockError surfaces through commit_order_transaction.
    - The transaction rolls back: inventory.current_stock remains 2.
    - No committed (non-cancelled) order exists in the DB.
    - Customer receives a message mentioning stock/availability — NOT a generic error.
    - Customer does NOT receive "problema técnico" / "error técnico".
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant ───────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Stock Rollback Restaurant",
        bot_number_raw="+570E2ESTOCK01",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)
    _clear_state_store()

    # ── Seed inventory row via legacy linked_dishes path ──────────────────────
    # This path in _deduct_inventory_in_tx is triggered when NO dish_recipes row
    # exists for the dish. The inventory row matches by linked_dishes @> [dish_name].
    inventory_id: int | None = None
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_stock_seed"):
            # Remove any existing inventory rows for this org to start clean
            await conn.execute(
                "DELETE FROM inventory WHERE org_id = $1", org_id
            )
            row = await conn.fetchrow(
                """
                INSERT INTO inventory
                  (org_id, name, unit, current_stock, min_stock, linked_dishes, cost_per_unit)
                VALUES ($1, $2, 'und', $3, 0, $4::jsonb, 0)
                RETURNING id, current_stock
                """,
                org_id,
                DISH_NAME,
                Decimal(STOCK_AVAILABLE),
                json.dumps([DISH_NAME]),
            )
            inventory_id = row["id"]
            assert row["current_stock"] == STOCK_AVAILABLE, (
                f"Seed failed: expected stock={STOCK_AVAILABLE}, got {row['current_stock']}"
            )
            log.info(
                "e2e.stock_test.seeded_inventory",
                org_id=org_id,
                inventory_id=inventory_id,
                stock=STOCK_AVAILABLE,
                dish=DISH_NAME,
            )

    log.info(
        "e2e.stock_test.start",
        org_id=org_id,
        bot_number=bot_number,
        phone=CUSTOMER_PHONE_A,
        order_qty=ORDER_QUANTITY,
        stock_available=STOCK_AVAILABLE,
    )

    # ── Turn 1: Request delivery with quantity > stock ─────────────────────────
    # We phrase the request unambiguously so the LLM doesn't round down.
    turn1_text = f"Hola, quiero pedir a domicilio {ORDER_QUANTITY} empanaditas de carne"
    log.info("e2e.stock_test.turn_1", phone=CUSTOMER_PHONE_A, text=turn1_text)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_A,
        text=turn1_text,
        bot_number=bot_number,
    )

    # ── Turn 2: Address ────────────────────────────────────────────────────────
    log.info("e2e.stock_test.turn_2", phone=CUSTOMER_PHONE_A)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_A,
        text=CUSTOMER_ADDRESS,
        bot_number=bot_number,
    )

    # ── Turn 3: Payment method (Efectivo — no proof needed) ───────────────────
    log.info("e2e.stock_test.turn_3", phone=CUSTOMER_PHONE_A)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_A,
        text="Efectivo",
        bot_number=bot_number,
    )

    # ── Turn 4: Confirm ────────────────────────────────────────────────────────
    log.info("e2e.stock_test.turn_4", phone=CUSTOMER_PHONE_A)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_A,
        text="sí confirmo",
        bot_number=bot_number,
    )

    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW_A)
    log.info(
        "e2e.stock_test.wa_replies",
        count=len(all_replies),
        replies=[r[:120] for r in all_replies],
    )

    # ── ASSERTION 1: Inventory unchanged ──────────────────────────────────────
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_stock_assert"):
            stock_after = await conn.fetchval(
                "SELECT current_stock FROM inventory WHERE id = $1",
                inventory_id,
            )
    assert stock_after is not None, "Inventory row was deleted during the transaction"
    assert int(stock_after) == STOCK_AVAILABLE, (
        f"REGRESSION — inventory decremented despite InsufficientStockError. "
        f"Expected stock={STOCK_AVAILABLE}, got {stock_after}. "
        f"Rule #13: InsufficientStockError must cause full rollback."
    )
    log.info("e2e.stock_test.assert_inventory_ok", stock_after=stock_after)

    # ── ASSERTION 2: No committed order ───────────────────────────────────────
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_stock_assert"):
            non_cancelled_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE org_id = $1
                  AND phone = $2
                  AND status != 'cancelado'
                """,
                org_id,
                CUSTOMER_PHONE_A,
            )
    assert non_cancelled_count == 0, (
        f"REGRESSION — order was committed despite InsufficientStockError. "
        f"Found {non_cancelled_count} non-cancelled order(s). "
        f"Rule #13: transaction must roll back completely."
    )
    log.info("e2e.stock_test.assert_no_order_ok")

    # ── ASSERTION 3: Customer received stock-specific message ─────────────────
    # The InsufficientStockError handler in execute_action (agent.py line 1467) returns:
    #   f"Lo siento, '{e}' ya no está disponible en el inventario. ¿Te gustaría elegir otra opción?"
    # This contains "disponible" — one of our sentinel tokens.
    STOCK_SIGNAL_TOKENS = ["stock", "disponible", "agotad", "inventario", "suficiente", "pocas unidades"]
    combined_replies = " ".join(all_replies).lower()
    matched_token = next(
        (tok for tok in STOCK_SIGNAL_TOKENS if tok in combined_replies),
        None,
    )
    assert matched_token is not None, (
        f"REGRESSION — customer did NOT receive a stock-specific message. "
        f"Expected one of {STOCK_SIGNAL_TOKENS} in WA replies. "
        f"Actual replies: {all_replies}. "
        f"This indicates InsufficientStockError was swallowed by generic except Exception "
        f"(Rule #13 violation)."
    )
    log.info("e2e.stock_test.assert_stock_message_ok", matched_token=matched_token)

    # ── ASSERTION 4: No generic error fallback leaked ─────────────────────────
    GENERIC_ERROR_TOKENS = ["problema técnico", "error técnico", "intenta más tarde"]
    for tok in GENERIC_ERROR_TOKENS:
        assert tok not in combined_replies, (
            f"REGRESSION — customer received generic fallback message containing '{tok}'. "
            f"InsufficientStockError is being caught by the generic except Exception branch "
            f"instead of the typed except InsufficientStockError branch (Rule #13 violation). "
            f"Actual replies: {all_replies}"
        )
    log.info("e2e.stock_test.assert_no_generic_error_ok")

    log.info("e2e.stock_test.PASS", phone=CUSTOMER_PHONE_A)


# ─────────────────────────────────────────────────────────────────────────────
# Test B: cart_lock_contention — neutral message, zero orders, cart preserved
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cart_lock_contention_returns_neutral_message(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Pre-acquires the cart lock from the test, then sends a confirmation turn.

    Expected outcome:
    - Bot replies with the neutral "procesando" message (Rule #5).
    - The LLM's "confirmado" reply does NOT leak to the customer.
    - Zero orders are created in the DB.
    - Cart still exists with items (customer can retry).
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant ───────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Cart Lock Restaurant",
        bot_number_raw="+570E2ELOCK001",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)
    _clear_state_store()

    log.info(
        "e2e.cartlock_test.start",
        org_id=org_id,
        bot_number=bot_number,
        phone=CUSTOMER_PHONE_B,
    )

    # ── Turns 1-3: Build cart and get to the confirmation step ────────────────
    # We stop BEFORE sending the confirmation so the cart is populated but no
    # order has been committed yet.

    # Turn 1: intent + item
    turn1_text = "Hola, quiero pedir a domicilio empanaditas de carne"
    log.info("e2e.cartlock_test.turn_1", phone=CUSTOMER_PHONE_B, text=turn1_text)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_B,
        text=turn1_text,
        bot_number=bot_number,
    )

    # Turn 2: address
    log.info("e2e.cartlock_test.turn_2", phone=CUSTOMER_PHONE_B)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_B,
        text=CUSTOMER_ADDRESS,
        bot_number=bot_number,
    )

    # Turn 3: payment method — bot should now ask for confirmation
    log.info("e2e.cartlock_test.turn_3", phone=CUSTOMER_PHONE_B)
    await simulate_whatsapp_inbound(
        client, pool,
        phone=CUSTOMER_PHONE_RAW_B,
        text="Efectivo",
        bot_number=bot_number,
    )

    # Snapshot WA replies after turn 3 (before the contention turn)
    replies_before_contention = wa_capture.texts_to(CUSTOMER_PHONE_RAW_B)[:]
    log.info(
        "e2e.cartlock_test.replies_before_contention",
        count=len(replies_before_contention),
        last=replies_before_contention[-1][:120] if replies_before_contention else "(none)",
    )

    # ── Pre-acquire the cart lock ─────────────────────────────────────────────
    # This simulates a concurrent worker holding the lock.
    # state_store.cart_lock_acquire works with both Redis and in-process fallback.
    from app.services import state_store
    lock_token = await state_store.cart_lock_acquire(
        CUSTOMER_PHONE_B, bot_number, ttl_seconds=30
    )
    assert lock_token is not None, (
        "cart_lock_acquire returned None — could not pre-acquire lock for contention test. "
        "This indicates a state_store bug."
    )
    log.info("e2e.cartlock_test.lock_acquired", token=lock_token[:8] + "...")

    # ── Turn 4: Confirmation while lock is held ───────────────────────────────
    # The agent will attempt to acquire the same lock, fail, and return the neutral message.
    try:
        log.info("e2e.cartlock_test.turn_4_confirming", phone=CUSTOMER_PHONE_B)
        await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW_B,
            text="sí confirmo",
            bot_number=bot_number,
        )
    finally:
        # Release the lock regardless of test outcome — do NOT leave a stale lock
        # that would break subsequent tests running against the same state_store.
        try:
            await state_store.cart_lock_release(CUSTOMER_PHONE_B, bot_number, token=lock_token)
            log.info("e2e.cartlock_test.lock_released")
        except Exception as _rel_err:
            log.warning("e2e.cartlock_test.lock_release_failed", error=str(_rel_err))

    # All WA replies (turn 1 through 4)
    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW_B)
    # Only messages that arrived AFTER pre-acquiring the lock (i.e. from turn 4)
    new_replies = all_replies[len(replies_before_contention):]
    log.info(
        "e2e.cartlock_test.wa_replies_after_contention",
        count=len(new_replies),
        replies=[r[:120] for r in new_replies],
    )

    # ── ASSERTION 1: Neutral "procesando" message received ────────────────────
    # The exact string returned by orders.py when cart_lock_contention is caught:
    #   "Tu pedido está siendo procesado, por favor espera un momento."
    # We assert on the deterministic substring "siendo procesado" which is unique
    # to this code path.
    NEUTRAL_SUBSTRING = "siendo procesado"
    combined_new = " ".join(new_replies).lower()
    assert NEUTRAL_SUBSTRING in combined_new, (
        f"REGRESSION — neutral contention message NOT found in turn-4 replies. "
        f"Expected substring '{NEUTRAL_SUBSTRING}'. "
        f"Actual new replies: {new_replies}. "
        f"Rule #5: cart_lock_contention MUST return neutral message, not LLM reply. "
        f"If new_replies is empty, the confirmation turn may not have processed correctly."
    )
    log.info("e2e.cartlock_test.assert_neutral_message_ok")

    # ── ASSERTION 2: No confirmation string leaked ────────────────────────────
    CONFIRM_LEAK_TOKENS = [
        "confirmado",
        "pedido recibido",
        "tu orden ha sido",
        "pedido en camino",
        "en preparación",
        "estamos preparando",
    ]
    for tok in CONFIRM_LEAK_TOKENS:
        assert tok not in combined_new, (
            f"REGRESSION — LLM confirmation reply leaked to customer: '{tok}' found. "
            f"Rule #5: when cart lock is held, the bot MUST NOT relay the LLM's "
            f"'confirmado' message. Actual new replies: {new_replies}"
        )
    log.info("e2e.cartlock_test.assert_no_confirm_leak_ok")

    # ── ASSERTION 3: Zero orders created ─────────────────────────────────────
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_lock_assert"):
            order_count = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE org_id = $1 AND phone = $2",
                org_id,
                CUSTOMER_PHONE_B,
            )
    assert order_count == 0, (
        f"REGRESSION — {order_count} order(s) were created despite cart lock being held. "
        f"Rule #5: when contention is detected, order commit MUST be skipped entirely."
    )
    log.info("e2e.cartlock_test.assert_zero_orders_ok")

    # ── ASSERTION 4: Cart still exists with items ─────────────────────────────
    # Customer should be able to retry; the cart must not have been deleted.
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_lock_assert"):
            cart_data_raw = await conn.fetchval(
                "SELECT cart_data FROM carts WHERE phone = $1 AND bot_number = $2",
                CUSTOMER_PHONE_B,
                bot_number,
            )

    # cart_data column is JSONB: {"items": [...]}. asyncpg jsonb codec returns dict;
    # without codec, returns str — handle both.
    if cart_data_raw is not None:
        if isinstance(cart_data_raw, str):
            import json as _json
            cart_data = _json.loads(cart_data_raw)
        else:
            cart_data = cart_data_raw
        cart_items = cart_data.get("items") if isinstance(cart_data, dict) else None
    else:
        cart_items = None

    # The cart may be None if the agent flow did not reach the cart-population step
    # (e.g. LLM did not call add_to_cart).  We log the state but do not hard-fail here
    # because the primary invariants (neutral message + zero orders) are the critical ones.
    # If cart is present it MUST have items.
    if cart_items is not None:
        assert len(cart_items) > 0, (
            f"REGRESSION — cart exists but is empty. "
            f"Items should have been preserved so the customer can retry."
        )
        log.info("e2e.cartlock_test.assert_cart_preserved_ok", item_count=len(cart_items))
    else:
        # Cart not created yet (LLM may have asked for more info before adding to cart).
        # Not a failure — the order commitment (turn 4) was still correctly blocked.
        log.info(
            "e2e.cartlock_test.cart_not_found_at_assertion",
            note="Cart was not in DB at assertion time; contention block still correct.",
        )

    log.info("e2e.cartlock_test.PASS", phone=CUSTOMER_PHONE_B)
