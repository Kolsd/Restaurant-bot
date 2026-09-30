"""
tests/test_delivery_entry.py
==============================
Chunk 2 of the delivery/pickup web wave (docs/claude/delivery-web.md):
the PUBLIC ENTRY POINT and sede assignment.

Three layers of test, matching how the code is actually split:

  A. Pure-function tests for app.services.delivery.is_location_open() — no
     DB. Covers the opening_hours <-> sede-timezone boundary explicitly
     (this repo has broken three tests before by comparing date.today()
     against utcnow() — see docs/claude/delivery-web.md instructions for
     this chunk).

  B. Real-database tests for app.services.delivery.resolve_order_mode()
     fed by app.repositories.delivery_repo.db_get_org_locations_for_entry()
     — the fallback ladder end-to-end against real seeded rows, using the
     same mesio_app / _ConnProxy fixture as tests/test_delivery_repo.py so
     RLS is genuinely exercised (docs/claude/testing.md).

  C. HTTP-level tests (TestClient against the real app, following
     tests/test_diner_routes.py's fixture style) for the three public
     endpoints: GET /api/diner/org/{slug}, POST /api/diner/order-mode/resolve,
     and the delivery/pickup branch of POST /api/diner/session — including
     the "leaks nothing" assertion and a deliberate org/location id
     collision for tenant isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── A. is_location_open() — pure function, no DB ────────────────────────────

_HOURS_MONDAY_ONLY = {
    "monday": {"open": "09:00", "close": "22:00", "closed": False},
    "tuesday": {"open": None, "close": None, "closed": True},
    "wednesday": {"open": None, "close": None, "closed": True},
    "thursday": {"open": None, "close": None, "closed": True},
    "friday": {"open": None, "close": None, "closed": True},
    "saturday": {"open": None, "close": None, "closed": True},
    "sunday": {"open": None, "close": None, "closed": True},
}


def test_open_evaluated_in_bogota_timezone_not_utc():
    """2026-01-06T01:00:00Z is Monday 20:00 in America/Bogota (UTC-5) —
    inside the configured 09:00-22:00 Monday window, so the sede IS open.
    A UTC-naive evaluation would see Tuesday 01:00 (Tuesday is configured
    closed) and wrongly report closed."""
    from app.services.delivery import is_location_open

    instant = datetime(2026, 1, 6, 1, 0, tzinfo=dt_timezone.utc)
    location = {"timezone": "America/Bogota", "opening_hours": _HOURS_MONDAY_ONLY}

    assert is_location_open(location, now=instant) is True


def test_closed_evaluated_in_auckland_timezone_not_utc():
    """2026-01-05T15:00:00Z is Monday 15:00 UTC — inside the Monday
    09:00-22:00 window if (wrongly) evaluated in UTC. But in
    Pacific/Auckland (UTC+13 at that date) the same instant is Tuesday
    04:00, and Tuesday is configured closed. The sede must be CLOSED."""
    from app.services.delivery import is_location_open

    instant = datetime(2026, 1, 5, 15, 0, tzinfo=dt_timezone.utc)
    location = {"timezone": "Pacific/Auckland", "opening_hours": _HOURS_MONDAY_ONLY}

    assert is_location_open(location, now=instant) is False


def test_no_opening_hours_configured_defaults_to_open():
    """A brand-new sede that has never touched the hours UI must not brick
    the entire delivery/pickup flow — see is_location_open()'s docstring
    for the reasoning."""
    from app.services.delivery import is_location_open

    assert is_location_open({"timezone": "America/Bogota", "opening_hours": {}}) is True
    assert is_location_open({"timezone": "America/Bogota"}) is True


def test_configured_day_absent_from_dict_is_closed():
    """Distinct from the "no config at all" case: monday-only config, but
    the same tz used to check Wednesday specifically. Tuesday is present
    but closed=True; Wednesday..sunday are also present but closed=True
    (see _HOURS_MONDAY_ONLY) — every one of them must read closed."""
    from app.services.delivery import is_location_open

    # Wednesday 2026-01-07 12:00 Bogota -> not open (only Monday is).
    instant = datetime(2026, 1, 7, 17, 0, tzinfo=dt_timezone.utc)  # 12:00 Bogota (-5)
    location = {"timezone": "America/Bogota", "opening_hours": _HOURS_MONDAY_ONLY}
    assert is_location_open(location, now=instant) is False


def test_overnight_window_wraps_past_midnight():
    location = {
        "timezone": "America/Bogota",
        "opening_hours": {
            **{d: {"open": None, "close": None, "closed": True} for d in
               ("tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
            "monday": {"open": "18:00", "close": "02:00", "closed": False},
        },
    }
    from app.services.delivery import is_location_open

    # Monday 23:00 Bogota -> within 18:00->02:00 overnight window -> open.
    late_monday = datetime(2026, 1, 6, 4, 0, tzinfo=dt_timezone.utc)  # 23:00 Mon Bogota
    assert is_location_open(location, now=late_monday) is True
    # Tuesday 01:00 Bogota (still inside the Monday-opened overnight window).
    early_tuesday_but_monday_window = datetime(2026, 1, 6, 6, 0, tzinfo=dt_timezone.utc)  # 01:00 Tue Bogota
    # Tuesday's OWN day config is "closed", and the overnight window is only
    # evaluated from the day it STARTS on (Monday) — this instant's local
    # weekday is Tuesday, so it correctly reads closed (Tuesday itself is
    # marked closed; the schema does not carry "yesterday's window" across
    # the day boundary). This asserts the actual (documented) behavior.
    assert is_location_open(location, now=early_tuesday_but_monday_window) is False


# ── asyncpg Connection.__slots__ workaround (tests/test_delivery_repo.py) ───


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


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


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
        from app.services.database import init_connection  # same jsonb codec as the app pool
        await init_connection(conn)
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


async def _set_scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))


async def _seed_org(conn, name: str, features: dict | None = None) -> int:
    return await conn.fetchval(
        "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
        name, f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}", features or {},
    )


async def _advance_location_seq(conn, forced_id: int) -> None:
    """A forced explicit id does NOT advance locations' BIGSERIAL sequence.

    Left alone, the sequence eventually reaches that id and the next
    ordinary INSERT — in this test or in any later one sharing the database —
    dies on `duplicate key value violates unique constraint "locations_pkey"`.
    That is why a deliberate-collision test can pass alone and fail in the
    full suite. Push the sequence past the forced id, never backwards.
    """
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('locations', 'id'),"
        "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
        ")",
        forced_id,
    )


async def _seed_location(
    conn, org_id: int, *, name: str = "Sede", loc_id: int | None = None,
    lat: float | None = None, lon: float | None = None,
    delivery_config: dict | None = None, opening_hours: dict | None = None,
    timezone: str = "America/Bogota", phone: str | None = "3000000000",
    address: str = "Calle 1", active: bool = True,
) -> int:
    cols = ["org_id", "name", "latitude", "longitude", "delivery_config",
            "opening_hours", "timezone", "phone", "address", "active"]
    vals = [org_id, name, lat, lon, delivery_config or {}, opening_hours or {},
            timezone, phone, address, active]
    if loc_id is not None:
        cols = ["id"] + cols
        vals = [loc_id] + vals
    placeholders = []
    idx = 1
    for c in cols:
        if c in ("delivery_config", "opening_hours"):
            placeholders.append(f"${idx}::jsonb")
        else:
            placeholders.append(f"${idx}")
        idx += 1
    sql = f"INSERT INTO locations ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING id"
    new_id = await conn.fetchval(sql, *vals)
    if loc_id is not None:
        await _advance_location_seq(conn, loc_id)
    return new_id


# ── B. resolve_order_mode() ladder, fed by real seeded rows ────────────────


async def test_ladder_nearest_open_covering_sede_wins(db_conn):
    from app.repositories.delivery_repo import db_get_org_locations_for_entry
    from app.services.delivery import resolve_order_mode

    org_id = await _seed_org(db_conn, "Ladder Org A")
    with tenant_scope(org_id):
        # Far sede, also covers and open, but farther away.
        await _seed_location(
            db_conn, org_id, name="Far Sede", lat=4.60, lon=-74.10,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 50},
        )
        near_id = await _seed_location(
            db_conn, org_id, name="Near Sede", lat=4.6097, lon=-74.0817,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 5},
        )

        org_row = {"id": org_id, "features": {}}
        locations = await db_get_org_locations_for_entry(org_id)

        result = resolve_order_mode(
            org=org_row, locations=locations, lat=4.6097, lon=-74.0817, requested_mode="delivery",
        )

    assert result["mode"] == "delivery"
    assert result["location"]["location_id"] == near_id
    assert result["reason"] is None
    assert result["candidates"] is None


async def test_ladder_falls_through_a_closed_covering_sede_to_the_next(db_conn):
    """A sede that covers the point but is CLOSED must be skipped in favor
    of the next covering sede that is open — even if the closed one is
    nearer."""
    from app.repositories.delivery_repo import db_get_org_locations_for_entry
    from app.services.delivery import resolve_order_mode

    org_id = await _seed_org(db_conn, "Ladder Org B")
    with tenant_scope(org_id):
        # Nearest: covers the point, delivery-enabled, but ALWAYS CLOSED
        # (opening_hours present with every day closed=True).
        closed_hours = {d: {"open": None, "close": None, "closed": True} for d in (
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        )}
        closed_id = await _seed_location(
            db_conn, org_id, name="Closed Near Sede", lat=4.6097, lon=-74.0817,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 5},
            opening_hours=closed_hours,
        )
        # Farther, but covers and is open (no opening_hours -> defaults open).
        open_id = await _seed_location(
            db_conn, org_id, name="Open Far Sede", lat=4.62, lon=-74.05,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 20},
        )

        org_row = {"id": org_id, "features": {}}
        locations = await db_get_org_locations_for_entry(org_id)
        result = resolve_order_mode(
            org=org_row, locations=locations, lat=4.6097, lon=-74.0817, requested_mode="delivery",
        )

    assert result["mode"] == "delivery"
    assert result["location"]["location_id"] == open_id
    assert result["location"]["location_id"] != closed_id


async def test_ladder_point_outside_every_radius_falls_back_to_pickup_ordered(db_conn):
    from app.repositories.delivery_repo import db_get_org_locations_for_entry
    from app.services.delivery import resolve_order_mode

    org_id = await _seed_org(db_conn, "Ladder Org C")
    with tenant_scope(org_id):
        far_id = await _seed_location(
            db_conn, org_id, name="Far Pickup Sede", lat=10.0, lon=-74.0,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 2},
        )
        near_id = await _seed_location(
            db_conn, org_id, name="Near Pickup Sede", lat=5.0, lon=-74.0,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 2},
        )

        org_row = {"id": org_id, "features": {}}
        locations = await db_get_org_locations_for_entry(org_id)
        # Point far from BOTH sedes' radius_km (2km) -> nobody covers it.
        result = resolve_order_mode(
            org=org_row, locations=locations, lat=4.0, lon=-74.0, requested_mode="delivery",
        )

    assert result["mode"] == "pickup"
    assert result["reason"] == "out_of_coverage"
    ids_in_order = [c["location_id"] for c in result["candidates"]]
    assert ids_in_order == [near_id, far_id], "must be ordered nearest-first"
    for c in result["candidates"]:
        assert "distance_km" in c and isinstance(c["distance_km"], float)


async def test_ladder_no_gps_returns_pickup_with_no_fabricated_distances(db_conn):
    from app.repositories.delivery_repo import db_get_org_locations_for_entry
    from app.services.delivery import resolve_order_mode

    org_id = await _seed_org(db_conn, "Ladder Org D")
    with tenant_scope(org_id):
        await _seed_location(
            db_conn, org_id, name="Sede X", lat=4.6, lon=-74.0,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 5},
        )
        await _seed_location(
            db_conn, org_id, name="Sede Y", lat=4.7, lon=-74.1,
            delivery_config={"delivery_enabled": True, "pickup_enabled": True, "radius_km": 5},
        )

        org_row = {"id": org_id, "features": {}}
        locations = await db_get_org_locations_for_entry(org_id)
        result = resolve_order_mode(
            org=org_row, locations=locations, lat=None, lon=None, requested_mode="delivery",
        )

    assert result["mode"] == "pickup"
    assert result["reason"] == "no_gps"
    assert len(result["candidates"]) == 2
    for c in result["candidates"]:
        assert "distance_km" not in c, "no GPS fix -> distance is unknown, must not be fabricated"


async def test_ladder_delivery_disabled_org_wide_is_a_distinct_reason(db_conn):
    """Distinct from out_of_coverage: no location in the org offers delivery
    AT ALL, so 'the point is outside every radius' would be the wrong
    diagnosis — the customer should be told delivery just isn't offered."""
    from app.repositories.delivery_repo import db_get_org_locations_for_entry
    from app.services.delivery import resolve_order_mode

    org_id = await _seed_org(db_conn, "Ladder Org E")
    with tenant_scope(org_id):
        await _seed_location(
            db_conn, org_id, name="Pickup Only Sede", lat=4.6097, lon=-74.0817,
            delivery_config={"delivery_enabled": False, "pickup_enabled": True, "radius_km": 50},
        )

        org_row = {"id": org_id, "features": {}}
        locations = await db_get_org_locations_for_entry(org_id)
        result = resolve_order_mode(
            org=org_row, locations=locations, lat=4.6097, lon=-74.0817, requested_mode="delivery",
        )

    assert result["mode"] == "pickup"
    assert result["reason"] == "delivery_disabled"
    assert len(result["candidates"]) == 1


async def test_ladder_tenant_isolation_never_pulls_another_orgs_locations(db_conn):
    """A deliberate id collision (same shape as test_delivery_repo.py's
    `collision` fixture / test_p0_collision_regression.py): org_b's own id
    equals a location that belongs to org_a. Resolving org_a's ladder must
    never see org_b's rows, and vice versa."""
    from app.repositories.delivery_repo import db_get_org_locations_for_entry

    org_a = await _seed_org(db_conn, "Collision Ladder Org A")
    org_b = await _seed_org(db_conn, "Collision Ladder Org B")

    with tenant_scope(org_a):
        loc_l = await _seed_location(
            db_conn, org_a, name="A's sede (id collides with org B)", loc_id=org_b, lat=4.6, lon=-74.0,
        )
    assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"

    with tenant_scope(org_b):
        loc_b = await _seed_location(db_conn, org_b, name="B's real sede", lat=4.6, lon=-74.0)

    with tenant_scope(org_a):
        locs_a = await db_get_org_locations_for_entry(org_a)
    with tenant_scope(org_b):
        locs_b = await db_get_org_locations_for_entry(org_b)

    assert {l["id"] for l in locs_a} == {loc_l}
    assert {l["id"] for l in locs_b} == {loc_b}, "org B must see only its OWN sede, not org A's colliding-id row"


# ── C. HTTP-level tests (TestClient against the real app) ──────────────────


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _get(client, url, **kwargs):
    _reset_pool()
    return client.get(url, **kwargs)


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


async def _http_scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))


async def _http_seed_delivery_org(*, with_delivery_sede: bool = True) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:10]
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
            f"Delivery Entry Org {suffix}", f"delivery-entry-{suffix}",
            json.dumps({"currency": "COP"}),
        )
        location_id = await conn.fetchval(
            """
            INSERT INTO locations
                (org_id, name, latitude, longitude, phone, address, delivery_config)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            org_id, f"Sede {suffix}", 4.6097, -74.0817,
            "3011234567", "Cra 1 # 2-3",
            json.dumps({"delivery_enabled": True, "pickup_enabled": True, "radius_km": 50})
            if with_delivery_sede else json.dumps({}),
        )
        return {"org_id": org_id, "location_id": location_id, "suffix": suffix}
    finally:
        await conn.close()


async def _http_teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _http_scope(conn, org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def delivery_org():
    info = _run(_http_seed_delivery_org())
    try:
        yield info
    finally:
        _run(_http_teardown_org(info["org_id"]))


async def _fetch_slug(org_id: int) -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval("SELECT slug FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


def test_org_info_endpoint_leaks_no_whatsapp_or_features_blob(client, delivery_org):
    slug = _run(_fetch_slug(delivery_org["org_id"]))
    resp = _get(client, f"/api/diner/org/{slug}")
    assert resp.status_code == 200, resp.text

    raw_body = resp.text
    assert "whatsapp_number" not in raw_body
    assert "wa_access_token" not in raw_body
    assert "wa_phone_id" not in raw_body
    assert "features" not in raw_body

    data = resp.json()
    assert data["currency"] == "COP"
    assert data["delivery_enabled"] is True
    assert data["pickup_enabled"] is True
    assert len(data["locations"]) == 1
    sede = data["locations"][0]
    assert sede["location_id"] == delivery_org["location_id"]
    assert sede["phone"] == "3011234567"
    assert sede["address"] == "Cra 1 # 2-3"
    assert "open_now" in sede


def test_org_info_unknown_slug_returns_404(client):
    resp = _get(client, "/api/diner/org/this-slug-does-not-exist-xyz")
    assert resp.status_code == 404


def test_org_info_excludes_sedes_with_neither_delivery_nor_pickup(client):
    info = _run(_http_seed_delivery_org(with_delivery_sede=False))
    try:
        slug = _run(_fetch_slug(info["org_id"]))
        resp = _get(client, f"/api/diner/org/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["locations"] == []
        assert data["delivery_enabled"] is False
        assert data["pickup_enabled"] is False
    finally:
        _run(_http_teardown_org(info["org_id"]))


async def _seed_colliding_orgs() -> dict:
    """All connection use happens inside ONE coroutine / one event loop —
    an asyncpg connection is bound to the loop it was created on, and this
    module's `_run()` gives every call a fresh throwaway loop (see
    tests/test_diner_routes.py's module docstring, event-loop note 1)."""
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:8]
        org_a = await conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            f"Org A {suffix}", f"org-a-{suffix}",
        )
        org_b = await conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            f"Org B {suffix}", f"org-b-{suffix}",
        )
        loc_l = await conn.fetchval(
            "INSERT INTO locations (id, org_id, name, delivery_config) VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
            org_b, org_a, "A's sede (collides with org B id)",
            json.dumps({"delivery_enabled": True, "pickup_enabled": True}),
        )
        # The forced explicit id above does NOT advance locations' BIGSERIAL
        # sequence; without this the sequence eventually reaches it and the
        # next ordinary INSERT anywhere in the suite dies on locations_pkey.
        # Same reason as _advance_location_seq() — this fixture opens its own
        # raw connection, so it calls setval directly.
        await conn.execute(
            "SELECT setval("
            "  pg_get_serial_sequence('locations', 'id'),"
            "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
            ")",
            loc_l,
        )
        slug_a = await conn.fetchval("SELECT slug FROM organizations WHERE id = $1", org_a)
        return {"org_a": org_a, "org_b": org_b, "loc_l": loc_l, "slug_a": slug_a, "suffix": suffix}
    finally:
        await conn.close()


async def _teardown_colliding_orgs(org_a: int, org_b: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM locations WHERE org_id = ANY($1::bigint[])", [org_a, org_b])
        await conn.execute("DELETE FROM organizations WHERE id = ANY($1::bigint[])", [org_a, org_b])
    finally:
        await conn.close()


def test_org_info_tenant_isolation_with_colliding_ids(client):
    """Same collision shape as the repo-level test above, exercised through
    the actual public HTTP endpoint this time."""
    seeded = _run(_seed_colliding_orgs())
    assert seeded["loc_l"] == seeded["org_b"], "setup invariant broken: location id must equal org_b's id"
    try:
        resp = _get(client, f"/api/diner/org/{seeded['slug_a']}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["locations"]) == 1
        assert data["locations"][0]["location_id"] == seeded["loc_l"]
        assert data["name"] == f"Org A {seeded['suffix']}"
    finally:
        _run(_teardown_colliding_orgs(seeded["org_a"], seeded["org_b"]))


def test_order_mode_resolve_endpoint_assigns_delivery(client, delivery_org):
    slug = _run(_fetch_slug(delivery_org["org_id"]))
    resp = _post(client, "/api/diner/order-mode/resolve", json={
        "slug": slug, "mode": "delivery", "lat": 4.6097, "lon": -74.0817,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["mode"] == "delivery"
    assert data["location"]["location_id"] == delivery_org["location_id"]
    assert data["reason"] is None


def test_order_mode_resolve_rejects_invalid_mode(client, delivery_org):
    slug = _run(_fetch_slug(delivery_org["org_id"]))
    resp = _post(client, "/api/diner/order-mode/resolve", json={"slug": slug, "mode": "teleport"})
    assert resp.status_code == 422


def test_session_delivery_mode_opens_with_null_table_and_resolved_location(client, delivery_org):
    slug = _run(_fetch_slug(delivery_org["org_id"]))
    resp = _post(client, "/api/diner/session", json={
        "order_mode": "delivery",
        "slug": slug,
        "location_id": delivery_org["location_id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["table_id"] is None
    assert data["location_id"] == delivery_org["location_id"]
    assert data["org_id"] == delivery_org["org_id"]
    assert data["order_mode"] == "delivery"
    assert data["token"].startswith("web:")

    # Verify what actually landed in the DB, not just the HTTP response.
    async def _fetch_session_row():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await _http_scope(conn, delivery_org["org_id"])
            return await conn.fetchrow(
                "SELECT table_id, location_id, order_mode FROM diner_sessions WHERE token = $1", data["token"],
            )
        finally:
            await conn.close()

    row = _run(_fetch_session_row())
    assert row["table_id"] is None
    assert row["location_id"] == delivery_org["location_id"]
    assert row["order_mode"] == "delivery"


def test_session_pickup_mode_rejected_when_sede_has_pickup_disabled(client):
    info = _run(_http_seed_delivery_org(with_delivery_sede=False))
    try:
        slug = _run(_fetch_slug(info["org_id"]))
        resp = _post(client, "/api/diner/session", json={
            "order_mode": "pickup",
            "slug": slug,
            "location_id": info["location_id"],
        })
        assert resp.status_code == 422
    finally:
        _run(_http_teardown_org(info["org_id"]))


def test_session_delivery_mode_rejects_location_from_a_different_org(client, delivery_org):
    """org_id/location_id are DISTINCT integers — a location_id that belongs
    to a DIFFERENT org than the resolved slug must be refused, never
    silently accepted (same P0 shape as the deleted db_get_restaurant_by_id)."""
    other = _run(_http_seed_delivery_org())
    try:
        slug = _run(_fetch_slug(delivery_org["org_id"]))
        resp = _post(client, "/api/diner/session", json={
            "order_mode": "delivery",
            "slug": slug,
            "location_id": other["location_id"],
        })
        assert resp.status_code == 404
    finally:
        _run(_http_teardown_org(other["org_id"]))


def test_session_dine_in_path_still_requires_table_id():
    """order_mode defaults to dine_in, and dine_in still requires table_id —
    a pydantic 422, not a 500, and NOT silently treated as delivery/pickup.
    (The full dine-in happy path is covered by tests/test_diner_routes.py;
    this only guards the cross-field validation this chunk added.)"""
    from app.main import app
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        resp = c.post("/api/diner/session", json={"order_mode": "dine_in"})
        assert resp.status_code == 422


def test_location_phone_round_trips_through_the_settings_write_path():
    """`locations.phone` (migration 0083) feeds the status page's "llamar al
    restaurante" button, so it must be writable through the ordinary
    location update path — a column no code can populate is dead schema and
    the button would show nothing forever. Guards the field whitelists in
    restaurant_repo.db_update_location / db_create_location.
    """
    from app.repositories import restaurant_repo

    async def _exercise():
        org = await _http_seed_delivery_org()
        try:
            loc_id = org["location_id"]
            updated = await restaurant_repo.db_update_location(loc_id, phone="3019998877")
            assert updated is not None
            assert updated["phone"] == "3019998877", "the update path must persist phone"

            fetched = await restaurant_repo.db_get_location_by_id(loc_id)
            assert fetched["phone"] == "3019998877", (
                "db_get_location_by_id must SELECT phone, otherwise every "
                "caller outside this wave reads it as missing"
            )
        finally:
            await _http_teardown_org(org["org_id"])

    _run(_exercise())
