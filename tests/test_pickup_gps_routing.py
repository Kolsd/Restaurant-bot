"""
tests/test_pickup_gps_routing.py
=================================
Integration tests for `restaurant_repo.db_resolve_location_by_gps` — the
haversine-based nearest-Location resolver.

Originally written for the WhatsApp pickup GPS branch-routing path in
agent_external.py (deleted in chunk 9, docs/claude/delivery-web.md —
delivery/pickup ordering moved entirely to the web channel). The routing
block itself and its "single-location auto-assign" test went with it; this
function stays alive as a general Org/Location repo primitive (also covered
by tests/test_org_repos.py) even though nothing calls it from the deleted
agent_external.py path any more. The web ordering flow's own GPS→sede
resolution (app/services/delivery.py::resolve_order_mode) reuses the same
underlying `restaurant_repo.haversine_km`, not this function.

What we verify:
  1. The resolver picks the nearest active Location within radius_km.
  2. It respects the radius cap — coordinates outside the radius return
     None, not the closest available.

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


async def _insert_location(conn, org_id_, *, name, lat, lon):
    row = await conn.fetchrow(
        """
        INSERT INTO locations
            (org_id, name, code, address, latitude, longitude, active)
        VALUES ($1, $2, $3, 'Test addr', $4, $5, TRUE)
        RETURNING id
        """,
        org_id_, name, name.lower().replace(" ", "-"), lat, lon,
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
        lat=4.7110, lon=-74.0721,
    )
    await _insert_location(
        db_conn, org_id, name="Sede Sur",
        lat=4.6000, lon=-74.0900,
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

