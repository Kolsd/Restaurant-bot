"""
tests/test_pickup_gps_routing.py
=================================
Integration tests for the pickup GPS branch-routing path in agent_external.py.

Background — the spec auditors thought GPS routing only fires on delivery.
Reality (as of agent_external.py:361): pickup ALSO routes by GPS when the
customer has shared their location AND the org has multiple sedes. The
auto-assigned location is then written to routing_context["location_id"]
so create_pickup_order picks it up.

What we verify:
  1. Pickup with GPS in cart auto-assigns the nearest active Location.
  2. Pickup without GPS on a single-Location org auto-assigns silently
     (the "single-location rule" — never ask "which sede?" when there's
     only one).
  3. db_resolve_location_by_gps respects the radius cap — coordinates
     outside the radius return None, not the closest available.

These tests exercise the repo helpers + the routing block in
execute_external_action directly (no LLM, no inbox worker). Full happy-path
GPS pickup is covered by tests/e2e/test_pickup_lifecycle.py.

Skipped when TEST_DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

# ── Skip module if no test DB ────────────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── _ConnProxy / _PoolShim (asyncpg __slots__ workaround) ────────────────────


class _ConnProxy:
    __slots__ = ("_c",)

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)

    async def execute(self, *a, **kw):
        return await object.__getattribute__(self, "_c").execute(*a, **kw)

    async def fetch(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetch(*a, **kw)

    async def fetchrow(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchrow(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchval(*a, **kw)

    def transaction(self, *a, **kw):
        return object.__getattribute__(self, "_c").transaction(*a, **kw)


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        async def _fake_get_pool():
            return shim

        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_id(db_conn):
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "PickupGpsOrg", f"pickup-gps-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _insert_location(conn, org_id_, *, name, lat, lon, whatsapp=None):
    row = await conn.fetchrow(
        """
        INSERT INTO locations
            (org_id, name, code, address, latitude, longitude,
             whatsapp_number, active)
        VALUES ($1, $2, $3, 'Test addr', $4, $5, $6, TRUE)
        RETURNING id
        """,
        org_id_, name, name.lower().replace(" ", "-"), lat, lon,
        whatsapp or "",
    )
    return row["id"]


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pickup_gps_resolves_to_nearest_location(db_conn, org_id):
    """db_resolve_location_by_gps picks the nearest active Location."""
    from app.repositories.restaurant_repo import db_resolve_location_by_gps

    # Two sedes in Bogotá: zona norte (~4.7110) and zona sur (~4.6000)
    norte_id = await _insert_location(
        db_conn, org_id, name="Sede Norte",
        lat=4.7110, lon=-74.0721, whatsapp="+5715551111",
    )
    await _insert_location(
        db_conn, org_id, name="Sede Sur",
        lat=4.6000, lon=-74.0900, whatsapp="+5715552222",
    )

    # Customer near Sede Norte (within ~150m)
    customer_lat, customer_lon = 4.7115, -74.0725
    nearest = await db_resolve_location_by_gps(
        org_id, customer_lat, customer_lon, radius_km=5.0
    )

    assert nearest is not None, "Expected a nearest location within 5 km"
    assert nearest["id"] == norte_id, (
        f"Expected Sede Norte (id={norte_id}), got id={nearest['id']}. "
        "GPS routing picked the wrong sede."
    )


@pytest.mark.asyncio
async def test_pickup_gps_outside_radius_returns_none(db_conn, org_id):
    """Customer further than radius_km from every Location → None."""
    from app.repositories.restaurant_repo import db_resolve_location_by_gps

    # Single sede in Bogotá
    await _insert_location(
        db_conn, org_id, name="Solo Sede",
        lat=4.7110, lon=-74.0721,
    )

    # Customer in Cali (~290 km away from Bogotá)
    cali_lat, cali_lon = 3.4516, -76.5320
    nearest = await db_resolve_location_by_gps(
        org_id, cali_lat, cali_lon, radius_km=5.0
    )

    assert nearest is None, (
        f"Expected None when customer is outside radius, got {nearest}. "
        "Radius cap not enforced — would route customer to a too-far sede."
    )


@pytest.mark.asyncio
async def test_pickup_single_location_no_gps_auto_assigns(db_conn, org_id):
    """Single-Location org with no GPS in cart → routing_context picks the only sede.

    This exercises the agent_external.py:370 branch: when org has exactly one
    Location, we auto-assign it without asking the customer "which sede?". Per
    the SINGLE-LOCATION rule in the system prompt.
    """
    from app.services.agent_external import execute_external_action
    from app.services.tenant_context import tenant_scope
    from app.services import orders as orders_service

    only_loc = await _insert_location(
        db_conn, org_id, name="Solo Sede",
        lat=4.7110, lon=-74.0721, whatsapp="+5715551111",
    )

    phone = "+573009990501"
    bot_number = "+5715551111"

    # RLS WITH CHECK on `carts` requires app.org_id GUC matching the row's org_id.
    await db_conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )

    # Seed cart with one item so the empty-cart guard doesn't short-circuit.
    # carts uses a single JSONB cart_data column (not separate items/etc).
    cart_row = await db_conn.fetchrow(
        """
        INSERT INTO carts (phone, bot_number, cart_data, org_id)
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING phone
        """,
        phone, bot_number,
        '{"items":[{"name":"Empanada","qty":1,"price":5000}],"order_type":null,"address":null,"notes":""}',
        org_id,
    )
    assert cart_row is not None

    routing_context: dict = {}

    # Skip the actual order creation — patch orders.create_order to short-circuit.
    async def _fake_create_order(*a, **kw):
        return {"success": False, "error": "test_short_circuit"}

    # Use monkeypatch via direct attribute (we're not in the monkeypatch fixture)
    real_create_order = orders_service.create_order
    orders_service.create_order = _fake_create_order
    try:
        with tenant_scope(org_id):
            await execute_external_action(
                parsed={
                    "action": "pickup",
                    "payment_method": "Nequi",
                },
                phone=phone,
                bot_number=bot_number,
                restaurant_obj={"id": org_id, "parent_restaurant_id": None},
                routing_context=routing_context,
                reply="",
            )
    finally:
        orders_service.create_order = real_create_order

    assert routing_context.get("location_id") == only_loc, (
        f"Expected routing_context.location_id={only_loc}, "
        f"got {routing_context.get('location_id')!r}. "
        "Single-Location org should auto-assign its only sede on pickup."
    )
    assert routing_context.get("branch_id") == only_loc, (
        "branch_id should be aliased to location_id for backward compat."
    )
