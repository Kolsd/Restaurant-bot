"""
tests/e2e/test_menu_86_dish_rejected.py — E2E test: 86'd dish refused by the bot.

An "86'd" dish is one marked unavailable in the `menu_availability` table.
The bot's system prompt includes `[NO DISPONIBLE]` next to such dishes via
`_build_compact_menu()` (agent.py). The LLM is instructed not to add unavailable
items to the cart.

NOTE ON ENFORCEMENT MODEL:
  `find_dish()` in orders.py does NOT hard-check `menu_availability`. The
  enforcement is purely via LLM context. This test validates that the LLM
  honours the `[NO DISPONIBLE]` marker and refuses to add the dish.

  If a hard enforcement layer is added to `add_to_cart()` in the future,
  this test continues to pass (the hard layer fires before the LLM reply).

Setup:
  1. Seed restaurant with 1 dish "Empanaditas de Carne" 15000
  2. INSERT into menu_availability: dish_name=DISH, org_id=ORG, available=FALSE
  3. Customer: "Hola, quiero pedir a domicilio 2 empanaditas"

Assertions:
  - orders table has ZERO rows for this phone (no order was committed)
  - Bot reply contains an unavailability keyword

Run:
  pytest tests/e2e/test_menu_86_dish_rejected.py -v -m e2e --tb=short
"""
from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

PHONE_RAW = "+573008880001"
PHONE = _normalize_phone(PHONE_RAW)

DISH_NAME = "Empanaditas de Carne"

MENU = {
    "Empanadas": [
        {
            "name": DISH_NAME,
            "description": "Empanadas fritas rellenas de carne y papa",
            "price": 15000,
            "active": True,
        }
    ]
}

PAYMENT_METHODS = ["Efectivo", "Nequi"]

# Keywords the bot should use when the dish is unavailable.
_UNAVAIL_KEYWORDS = [
    "no disponible", "disponible", "agotad", "temporalment",
    "actualmente", "momento", "sin stock",
]


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


# ── Test ───────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_86d_dish_rejected_by_bot(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Marks a dish as unavailable in menu_availability, then asks the bot for it.

    Expected:
      - No order is committed (orders COUNT == 0)
      - Bot reply mentions unavailability

    If both assertions fail, the LLM ignored [NO DISPONIBLE] — that is a prompt
    compliance regression (the hard enforcement layer is not present in orders.py).
    """
    client = e2e_app
    pool = test_pool

    # ── Seed restaurant ───────────────────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E 86 Dish Restaurant",
        bot_number_raw="+570E2E86DISH1",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    await truncate_e2e_data(pool, org_id)

    # ── Mark dish as unavailable in menu_availability ─────────────────────────
    # db_set_dish_availability uses ON CONFLICT (dish_name, org_id) DO UPDATE.
    # We insert directly to avoid importing the service layer with tenant scope setup.
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_86_seed"):
            # Clean up any leftover availability row from prior runs.
            # PRIMARY KEY is (dish_name) — delete by dish_name only.
            await conn.execute(
                "DELETE FROM menu_availability WHERE dish_name = $1",
                DISH_NAME,
            )
            # Primary key on menu_availability is (dish_name) — not (dish_name, org_id).
            # The org_id column exists but the unique constraint is on dish_name alone.
            await conn.execute(
                """
                INSERT INTO menu_availability (dish_name, org_id, available, updated_at)
                VALUES ($1, $2, FALSE, NOW())
                ON CONFLICT (dish_name) DO UPDATE SET
                    org_id    = EXCLUDED.org_id,
                    available = FALSE,
                    updated_at = NOW()
                """,
                DISH_NAME, org_id,
            )
            log.info("e2e.86.marked_unavailable", dish=DISH_NAME, org_id=org_id)

    # ── Customer asks for the 86'd dish ───────────────────────────────────────
    await simulate_whatsapp_inbound(
        client, pool,
        phone=PHONE_RAW,
        text="Hola, quiero pedir a domicilio 2 empanaditas de carne",
        bot_number=bot_number,
    )

    all_replies = wa_capture.texts_to(PHONE_RAW)
    combined = " ".join(all_replies).lower()

    log.info(
        "e2e.86.replies",
        count=len(all_replies),
        replies=[r[:120] for r in all_replies],
    )

    # ── ASSERTION 1: No order committed ──────────────────────────────────────
    async with pool.acquire() as conn:
        with bypass_tenant_scope("e2e_86_assert"):
            order_count = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE org_id = $1 AND phone = $2",
                org_id, PHONE,
            )

    assert order_count == 0, (
        f"REGRESSION: An order was committed for an 86'd dish. "
        f"Found {order_count} order(s) for phone={PHONE}. "
        f"The [NO DISPONIBLE] marker in the system prompt should prevent the LLM "
        f"from calling create_delivery_order for unavailable dishes. "
        f"Bot replies: {all_replies}"
    )
    log.info("e2e.86.assert_no_order_ok")

    # ── ASSERTION 2: Bot mentioned unavailability ─────────────────────────────
    assert len(all_replies) >= 1, (
        f"Bot sent no reply to the customer at all. "
        f"Phone={PHONE_RAW}, bot_number={bot_number}."
    )

    matched = next((kw for kw in _UNAVAIL_KEYWORDS if kw in combined), None)
    assert matched is not None, (
        f"Bot did not mention unavailability. "
        f"Expected one of {_UNAVAIL_KEYWORDS} in the combined reply. "
        f"Actual replies: {all_replies}. "
        f"The LLM may have ignored the [NO DISPONIBLE] marker in the menu context. "
        f"Check _build_compact_menu() (agent.py) and the system prompt."
    )
    log.info("e2e.86.assert_unavail_message_ok", matched_keyword=matched)

    print(
        f"\n[OK] 86'd dish correctly refused by bot.\n"
        f"  org_id={org_id}, dish={DISH_NAME}\n"
        f"  orders committed: {order_count}\n"
        f"  unavailability keyword found: '{matched}'"
    )
