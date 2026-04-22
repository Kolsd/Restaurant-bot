"""
tests/e2e/test_delivery_gps_outside_radius.py — E2E: GPS coordinates outside 5km delivery radius.

Feature under test:
  agent_external.py execute_external_action() reads GPS from cart when the LLM calls
  create_delivery_order. It calls db_resolve_location_by_gps(org_id, lat, lon, radius_km=5.0).
  When the coordinates are outside the 5km radius and the org has GPS-enabled locations,
  the function returns the out-of-radius message BEFORE creating the order:
    "Lo siento mucho, verificamos tu ubicación GPS y estás fuera de nuestra zona de
     cobertura para domicilios."
  The order is NOT committed.

Flow architecture:
  - Turn 1: intent + item (LLM responds asking for address)
  - Turn 2a: GPS location message → inbox_worker saves lat/lon to cart
  - Turn 2b: text address (so LLM accepts the address step, uses GPS from cart for routing)
  - Turn 3: payment method
  - Turn 4: "confirmo" → LLM fires create_delivery_order → execute_external_action
    reads GPS from cart, finds it outside radius, returns rejection. Order NOT committed.

Exploration notes (2026-04-21):
  agent_external.py lines 180-230:
    cart_data = await db.db_get_cart(phone, bot_number)
    if cart_data.get("latitude") is not None: has_gps = True
    nearest_loc = await db_resolve_location_by_gps(org_id, lat, lon, radius_km=5.0)
    if nearest_loc is None and locs_with_gps and has_gps:
      return "fuera de nuestra zona de cobertura..."

Run:
  pytest tests/e2e/test_delivery_gps_outside_radius.py -v -s -m e2e
"""
from __future__ import annotations

import asyncio
import re
import time

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

# ── Constants ──────────────────────────────────────────────────────────────────

CUSTOMER_PHONE_RAW = "+573009991100"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

# Branch is in Bogotá zona norte.
BRANCH_LAT = 4.710989
BRANCH_LON = -74.072092

# Customer GPS is far south-east — well over 100 km away from the branch.
# Straight-line distance to branch: sqrt((4.71 - 4.10)^2 * (111km/deg)^2 + ...) ≈ 115 km
CUSTOMER_LAT = 4.100000
CUSTOMER_LON = -73.000000

# Text address sent so the LLM doesn't stall asking for address
CUSTOMER_ADDRESS = "Calle Falsa 123, Leticia, Amazonas"

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

# Substrings the bot is expected to include in its out-of-radius reply.
# These come from agent_external.py lines 226-230.
_OUT_OF_RADIUS_KEYWORDS = re.compile(
    r"fuera|no cubrimos|rango|radio|zona|cobertura|no podemos entregar",
    re.IGNORECASE,
)


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
async def test_delivery_gps_outside_radius(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    GPS coordinates far outside 5km radius → bot refuses delivery at order creation, no order committed.

    The GPS lat/lon is saved to the cart on the location-type message turn. The radius
    check fires in execute_external_action when the LLM calls create_delivery_order
    (after payment + confirm turns). The function returns an early rejection string
    without committing any order to the DB.

    Assertions:
      - Bot reply eventually contains an out-of-radius phrase.
      - No 'domicilio' order committed in DB for this customer.
    """
    client = e2e_app
    pool = test_pool

    restaurant = await seed_restaurant(
        pool,
        name="E2E GPS Radius Test Restaurant",
        bot_number_raw="+570E2EGPSRADIUS",
        menu=MENU,
        payment_methods=PAYMENT_METHODS,
        num_branches=1,
        branch_latlons=[(BRANCH_LAT, BRANCH_LON)],
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

    log.info(
        "e2e.gps_radius.test_start",
        parent_id=parent_id,
        bot_number=bot_number,
        customer_phone=CUSTOMER_PHONE,
        customer_gps=(CUSTOMER_LAT, CUSTOMER_LON),
        branch_gps=(BRANCH_LAT, BRANCH_LON),
    )

    async def _turn(text: str = "", lat=None, lon=None, turn_name: str = "") -> list[str]:
        t_start = time.monotonic()
        processed = await simulate_whatsapp_inbound(
            client, pool,
            phone=CUSTOMER_PHONE_RAW,
            text=text,
            bot_number=bot_number,
            lat=lat,
            lon=lon,
        )
        elapsed = round(time.monotonic() - t_start, 1)
        replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
        log.info(f"e2e.gps_radius.{turn_name}_done",
                 processed=processed, elapsed_s=elapsed, reply_count=len(replies))
        assert processed >= 1, f"{turn_name}: inbox item was not processed"
        return replies

    # ── Turn 1: delivery intent + item ────────────────────────────────────────
    replies_1 = await _turn(
        text="Hola, quiero pedir a domicilio empanaditas",
        turn_name="turn_1",
    )
    assert len(replies_1) >= 1, "Turn 1: bot sent no WA message"

    # ── Turn 2a: GPS location (far outside radius) → saved to cart ────────────
    # The bot receives a location-type message; inbox_worker persists lat/lon to cart.
    # The bot then asks for the address (or just the next step) since it's in the
    # STEP 3 of the strict sales funnel.
    replies_2a = await _turn(
        lat=CUSTOMER_LAT,
        lon=CUSTOMER_LON,
        turn_name="turn_2a_gps",
    )
    assert len(replies_2a) >= 2, "Turn 2a GPS: bot sent no reply"

    # Check if the radius rejection already fired here.
    # It can fire here IF the bot was already in the address step and the GPS
    # location triggers execute_external_action immediately.
    latest_2a = replies_2a[-1]
    log.info("e2e.gps_radius.turn_2a_reply", text=latest_2a[:200])

    if _OUT_OF_RADIUS_KEYWORDS.search(latest_2a):
        log.info("e2e.gps_radius.rejection_at_turn_2a")
        # Radius rejection already returned — no further turns needed
        _verify_no_order(pool, parent_id)
        return

    # ── Turn 2b: Text address (fallback path if GPS alone didn't trigger) ──────
    replies_2b = await _turn(
        text=f"Mi dirección es {CUSTOMER_ADDRESS}",
        turn_name="turn_2b_address",
    )
    assert len(replies_2b) >= 3, "Turn 2b address: bot sent no reply"

    latest_2b = replies_2b[-1]
    if _OUT_OF_RADIUS_KEYWORDS.search(latest_2b):
        log.info("e2e.gps_radius.rejection_at_turn_2b")
        await _verify_no_order(pool, parent_id)
        return

    # ── Turn 3: Payment method ────────────────────────────────────────────────
    replies_3 = await _turn(text="Efectivo", turn_name="turn_3")
    assert len(replies_3) >= 4, "Turn 3: bot sent no reply after payment"

    # ── Turn 4: Confirmation → triggers create_delivery_order → radius check ──
    replies_4 = await _turn(text="sí confirmo", turn_name="turn_4")

    # At this point the LLM should have called create_delivery_order.
    # execute_external_action reads lat/lon from cart, finds distance > 5km,
    # and returns the rejection string. The LLM receives it and relays it.
    await asyncio.sleep(0.5)

    # Check all replies from all turns for the out-of-radius message
    all_replies = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    out_of_radius_replies = [t for t in all_replies if _OUT_OF_RADIUS_KEYWORDS.search(t)]

    log.info(
        "e2e.gps_radius.final_reply_check",
        total_replies=len(all_replies),
        out_of_radius_replies=len(out_of_radius_replies),
        last_3=all_replies[-3:],
    )

    assert out_of_radius_replies, (
        f"Expected bot to mention out-of-radius delivery restriction at some point. "
        f"All {len(all_replies)} WA replies to customer: {all_replies}. "
        f"Known keywords: fuera, no cubrimos, rango, radio, zona, cobertura. "
        f"GPS was ({CUSTOMER_LAT}, {CUSTOMER_LON}), branch at ({BRANCH_LAT}, {BRANCH_LON}) — "
        f"distance is >>5 km."
    )

    # ── Assert no order was committed ─────────────────────────────────────────
    await _verify_no_order(pool, parent_id)

    log.info(
        "e2e.gps_radius.test_passed",
        parent_id=parent_id,
        total_wa_messages=len(wa_capture.messages),
    )
    print(
        f"\n[OK] E2E GPS outside radius test passed: "
        f"bot correctly refused delivery for GPS ({CUSTOMER_LAT}, {CUSTOMER_LON}), "
        f"0 orders committed."
    )


async def _verify_no_order(pool: asyncpg.Pool, parent_id: int) -> None:
    """Assert no delivery order was committed for this customer."""
    await asyncio.sleep(0.3)
    with bypass_tenant_scope("e2e_gps_radius_assert_no_order"):
        async with pool.acquire() as conn:
            order_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM orders
                WHERE phone = $1
                  AND order_type = 'domicilio'
                """,
                CUSTOMER_PHONE,
            )

    assert order_count == 0, (
        f"Expected 0 delivery orders for out-of-radius customer, "
        f"but found {order_count}. The bot committed an order despite GPS being outside "
        f"the 5km delivery radius — this is a real bug."
    )
