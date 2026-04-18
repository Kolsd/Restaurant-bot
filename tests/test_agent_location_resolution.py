"""
tests/test_agent_location_resolution.py

Unit tests for location_id propagation through the agent pipeline (Bloque S4):
  - delivery: GPS resolves Location via db_resolve_location_by_gps
  - delivery: out-of-coverage returns friendly reply
  - pickup single Location: auto-picks without asking
  - pickup multiple Locations: asks customer to pick
  - conversation persists location_id after resolution
  - chat() accepts location_id kwarg and threads it through

Tests:
  1. test_delivery_resolves_location_by_gps_within_radius
  2. test_delivery_out_of_coverage_replies_friendly
  3. test_pickup_single_location_auto_picks
  4. test_pickup_multiple_locations_asks_user
  5. test_conversation_persists_location_id_once_resolved
  6. test_chat_accepts_location_id_kwarg
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_restaurant_obj(org_id: int = 1, has_parent: bool = False) -> dict:
    return {
        "id": org_id,
        "name": "Test Org",
        "menu": {},
        "features": {},
        "whatsapp_number": "573001234567",
        "wa_access_token": "test_token",
        "wa_phone_id": "ph1",
        "parent_restaurant_id": 999 if has_parent else None,  # None = Org-level (Matriz)
    }


def _make_location(loc_id: int, org_id: int = 1, name: str = "Sede X") -> dict:
    return {
        "id": loc_id,
        "org_id": org_id,
        "name": name,
        "address": f"Calle {loc_id}",
        "latitude": 4.71 + loc_id * 0.01,
        "longitude": -74.07 + loc_id * 0.01,
        "whatsapp_number": None,
        "is_primary": True if loc_id == 10 else False,
        "distance_km": 1.2,
    }


# ── Test 1: delivery resolves Location via GPS ────────────────────────────────

@pytest.mark.asyncio
async def test_delivery_resolves_location_by_gps_within_radius():
    """execute_external_action resolves Location via db_resolve_location_by_gps
    when GPS coordinates are available in the cart and the location is within 5km.
    routing_context["location_id"] must be set.
    """
    from app.services.agent_external import execute_external_action

    restaurant_obj = _make_restaurant_obj(org_id=1)
    routing_context = {}

    # Cart has GPS
    cart_data = {"items": [{"name": "Bandeja", "qty": 1}], "latitude": 4.71, "longitude": -74.07}

    nearest_loc = _make_location(10, org_id=1)

    parsed = {
        "action": "delivery",
        "address": "Mi ubicación GPS",
        "payment_method": "efectivo",
        "items": [],
    }

    with (
        patch("app.services.database.db_get_cart", AsyncMock(return_value=cart_data)),
        patch(
            "app.repositories.restaurant_repo.db_get_org_locations",
            AsyncMock(return_value=[nearest_loc]),
        ),
        patch(
            "app.repositories.restaurant_repo.db_resolve_location_by_gps",
            AsyncMock(return_value=nearest_loc),
        ),
        # db_find_nearest_branch should NOT be called (Org routing takes over)
        patch("app.services.database.db_find_nearest_branch", AsyncMock(return_value=None)),
        # Cart check for empty order guard
        patch("app.services.database.db_get_cart", AsyncMock(return_value=cart_data)),
        # Order creation — success
        patch(
            "app.services.orders.create_order",
            AsyncMock(return_value={"success": True, "order": {"id": 1, "total": 28000, "is_additional": False}}),
        ),
    ):
        await execute_external_action(parsed, "5551234", "573001234567", restaurant_obj, routing_context, "Pedido confirmado")

    assert routing_context.get("location_id") == 10


# ── Test 2: delivery out-of-coverage replies friendly ─────────────────────────

@pytest.mark.asyncio
async def test_delivery_out_of_coverage_replies_friendly():
    """When GPS is present but no Location within 5km, returns friendly no-coverage reply."""
    from app.services.agent_external import execute_external_action

    restaurant_obj = _make_restaurant_obj(org_id=1)
    routing_context = {}

    cart_data = {"items": [], "latitude": 10.0, "longitude": -75.0}  # Far away
    nearby_loc = _make_location(10, org_id=1)

    parsed = {
        "action": "delivery",
        "address": "Mi GPS",
        "payment_method": "efectivo",
        "items": [],
    }

    with (
        patch("app.services.database.db_get_cart", AsyncMock(return_value=cart_data)),
        patch(
            "app.repositories.restaurant_repo.db_get_org_locations",
            AsyncMock(return_value=[nearby_loc]),  # location has GPS → out of coverage
        ),
        patch(
            "app.repositories.restaurant_repo.db_resolve_location_by_gps",
            AsyncMock(return_value=None),  # out of range
        ),
        patch("app.services.database.db_find_nearest_branch", AsyncMock(return_value=None)),
    ):
        reply = await execute_external_action(
            parsed, "5551234", "573001234567", restaurant_obj, routing_context, ""
        )

    assert "cobertura" in reply.lower() or "zona" in reply.lower() or "fuera" in reply.lower()


# ── Test 3: pickup single Location auto-picks ─────────────────────────────────

@pytest.mark.asyncio
async def test_pickup_single_location_auto_picks():
    """Pickup with single Location auto-assigns without asking customer."""
    from app.services.agent_external import execute_external_action

    restaurant_obj = _make_restaurant_obj(org_id=1)
    routing_context = {}

    only_loc = _make_location(10, org_id=1, name="Sede Única")
    cart_data = {"items": [{"name": "Bandeja", "qty": 1}]}

    parsed = {
        "action": "pickup",
        "address": "",
        "payment_method": "nequi",
        "items": [],
    }

    with (
        patch("app.services.database.db_get_cart", AsyncMock(return_value=cart_data)),
        patch(
            "app.repositories.restaurant_repo.db_get_org_locations",
            AsyncMock(return_value=[only_loc]),
        ),
        patch(
            "app.services.orders.create_order",
            AsyncMock(return_value={"success": True, "order": {"id": 2, "total": 28000, "is_additional": False}}),
        ),
    ):
        reply = await execute_external_action(
            parsed, "5551234", "573001234567", restaurant_obj, routing_context, ""
        )

    # location_id auto-assigned
    assert routing_context.get("location_id") == 10
    # Should NOT ask which branch
    assert "sede" not in reply.lower() or "registrado" in reply.lower()


# ── Test 4: pickup multiple Locations asks customer ───────────────────────────

@pytest.mark.asyncio
async def test_pickup_multiple_locations_asks_user():
    """Pickup with multiple Locations and no GPS returns a branch-selection reply."""
    from app.services.agent_external import execute_external_action

    restaurant_obj = _make_restaurant_obj(org_id=1)
    routing_context = {}

    loc_a = _make_location(10, org_id=1, name="Sede Norte")
    loc_b = _make_location(11, org_id=1, name="Sede Sur")
    cart_data = {"items": [{"name": "Bandeja", "qty": 1}]}  # no latitude/longitude

    parsed = {
        "action": "pickup",
        "address": "",
        "payment_method": "nequi",
        "items": [],
        "branch_id": 0,
    }

    with (
        patch("app.services.database.db_get_cart", AsyncMock(return_value=cart_data)),
        patch(
            "app.repositories.restaurant_repo.db_get_org_locations",
            AsyncMock(return_value=[loc_a, loc_b]),
        ),
    ):
        reply = await execute_external_action(
            parsed, "5551234", "573001234567", restaurant_obj, routing_context, ""
        )

    # Should ask customer to pick a sede
    assert "sede" in reply.lower() or "sucursal" in reply.lower()
    # location_id NOT set (not resolved yet)
    assert "location_id" not in routing_context


# ── Test 5: conversation persists location_id once resolved ───────────────────

@pytest.mark.asyncio
async def test_conversation_persists_location_id_once_resolved():
    """db_save_history is called with location_id; the execute SQL includes location_id param."""
    from app.repositories.conversations_repo import db_save_history
    from app.services.tenant_context import tenant_scope

    # Build a proper asyncpg conn mock following test_loyalty_repo_tenant.py pattern
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    # transaction() must be a sync MagicMock whose __aenter__/__aexit__ are async
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)

    with (
        patch("app.services.database.get_pool", AsyncMock(return_value=pool)),
        tenant_scope(42),
    ):
        await db_save_history(
            phone="5551234",
            bot_number="bot123",
            history=[{"role": "user", "content": "hola"}],
            location_id=10,
        )

    # Verify execute was called with the location_id (10) as a positional arg
    execute_calls = conn.execute.call_args_list
    # set_config call + the INSERT call
    insert_call = next(
        (c for c in execute_calls if "INSERT INTO conversations" in str(c.args[0])),
        None,
    )
    assert insert_call is not None, "No INSERT INTO conversations execute call found"
    # positional args: (sql, phone, bot_number, history_json, branch_id, location_id)
    insert_args = insert_call.args
    assert 10 in insert_args, f"location_id=10 not found in execute args: {insert_args}"


# ── Test 6: chat() accepts location_id kwarg ─────────────────────────────────

@pytest.mark.asyncio
async def test_chat_accepts_location_id_kwarg():
    """chat() must accept location_id keyword argument without raising TypeError."""
    from app.services.agent import chat
    import inspect

    sig = inspect.signature(chat)
    assert "location_id" in sig.parameters, "chat() must accept location_id kwarg"
    assert sig.parameters["location_id"].default is None, "location_id must default to None"
