"""
tests/test_delivery_repo.py
=============================
Integration tests for app/repositories/delivery_repo.py against a real
Postgres test database (TEST_DATABASE_URL). No mocked repo — every
assertion checks actual rows/columns written by real SQL.

Fixture pattern is the one documented in docs/claude/testing.md
("Integration test pattern against TEST_DATABASE_URL"): a single real
asyncpg connection wrapped in _ConnProxy/_PoolShim, get_pool() monkeypatched
to return it, everything inside one rolled-back transaction with
`SET LOCAL ROLE mesio_app` so RLS actually enforces (the test DB connects as
postgres/superuser by default, which would silently bypass FORCE RLS).

One addition versus the reference fixture (test_loyalty_aggregates.py):
delivery_repo writes locations.delivery_config as a `$n::jsonb` parameter
carrying a raw Python dict (never json.dumps()'d — see the module docstring
in delivery_repo.py for why pre-dumping double-encodes). That only works
when the connection has the SAME jsonb codec the real app pool registers
(app/services/database.py: encoder=json.dumps, decoder=json.loads). The raw
test connection has no such codec by default, so this file registers it
explicitly on the acquired connection before wrapping it.

Tenant isolation is tested with a DELIBERATE id collision (same shape as
tests/test_p0_collision_regression.py): org B's own id is forced to equal a
location that actually belongs to org A. Every isolation assertion below
exists specifically to prove that collision cannot cross-contaminate reads
or writes.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── asyncpg Connection.__slots__ workaround (see docs/claude/testing.md) ────


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


# ── Fixtures ──────────────────────────────────────────────────────────────


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
        # Match the real pool's jsonb codec (app/services/database.py) so
        # delivery_repo's raw-dict `$n::jsonb` params encode exactly once,
        # the same way they will in production.
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


# ── Seed helpers ─────────────────────────────────────────────────────────


async def _seed_org(conn, name: str) -> int:
    return await conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        name, f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
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


async def _seed_location(conn, org_id: int, name: str = "Sede Principal", loc_id: int | None = None) -> int:
    if loc_id is not None:
        new_id = await conn.fetchval(
            "INSERT INTO locations (id, org_id, name) VALUES ($1, $2, $3) RETURNING id",
            loc_id, org_id, name,
        )
        await _advance_location_seq(conn, loc_id)
        return new_id
    return await conn.fetchval(
        "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
        org_id, name,
    )


async def _seed_staff(conn, org_id: int, location_id: int | None = None, role: str = "cajero") -> str:
    # staff has RLS + FORCE too.
    await _set_scope(conn, org_id)
    staff_id = await conn.fetchval(
        "INSERT INTO staff (name, role, username, org_id, location_id) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        f"Staff {uuid.uuid4().hex[:6]}", role, f"staff_{uuid.uuid4().hex[:10]}", org_id, location_id,
    )
    return str(staff_id)


async def _seed_order(conn, *, org_id: int, location_id: int, status: str = "pendiente_aceptacion", **overrides) -> str:
    # orders has RLS + FORCE — WITH CHECK requires app.org_id to already
    # match the row being inserted (docs/claude/testing.md gotcha #7).
    await _set_scope(conn, org_id)
    row = {
        "id": f"ord-{uuid.uuid4().hex[:12]}",
        "phone": f"web:{uuid.uuid4().hex}",
        "items": [{"name": "Bandeja Paisa", "qty": 1}],
        "order_type": "domicilio",
        "subtotal": Decimal("20000"),
        "total": Decimal("20000"),
        "org_id": org_id,
        "location_id": location_id,
        "status": status,
    }
    row.update(overrides)
    cols = list(row.keys())
    placeholders = [f"${i}::jsonb" if c == "items" else f"${i}" for i, c in enumerate(cols, start=1)]
    sql = f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING id"
    return await conn.fetchval(sql, *[row[c] for c in cols])


async def _fetch_order(conn, order_id: str) -> dict:
    row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
    return dict(row) if row else None


def _recent_utc(ts) -> bool:
    assert ts is not None, "expected a timestamp, got None"
    assert ts.tzinfo is not None, "delivery-lifecycle timestamps must be timezone-aware (UTC)"
    delta = abs((datetime.now(timezone.utc) - ts).total_seconds())
    return delta < 30


# ── Location delivery config ────────────────────────────────────────────


async def test_set_and_get_location_delivery_config_roundtrip(db_conn):
    from app.repositories.delivery_repo import (
        db_get_location_delivery_config,
        db_set_location_delivery_config,
    )

    org_id = await _seed_org(db_conn, "Config Org")
    location_id = await _seed_location(db_conn, org_id)

    config = {
        "delivery_enabled": True,
        "pickup_enabled": False,
        "delivery_fee": 4500.0,
        "min_order": 15000.0,
        "radius_km": 6.0,
        "prep_minutes": 25,
        "payment_methods": ["efectivo", "nequi"],
    }

    with tenant_scope(org_id):
        updated = await db_set_location_delivery_config(org_id, location_id, config)
        fetched = await db_get_location_delivery_config(org_id, location_id)

    assert updated is not None
    assert updated["delivery_config"]["delivery_fee"] == 4500.0
    assert fetched == config

    # Confirm the raw column really holds a dict, not a double-encoded string.
    raw = await db_conn.fetchval("SELECT delivery_config FROM locations WHERE id = $1", location_id)
    assert isinstance(raw, dict), f"delivery_config was double-encoded, got {type(raw)}: {raw!r}"
    assert raw["payment_methods"] == ["efectivo", "nequi"]


async def test_get_location_delivery_config_defaults_to_empty_dict(db_conn):
    from app.repositories.delivery_repo import db_get_location_delivery_config

    org_id = await _seed_org(db_conn, "Fresh Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        config = await db_get_location_delivery_config(org_id, location_id)

    assert config == {}


async def test_set_location_delivery_config_wrong_org_is_refused(db_conn):
    """A location must belong to org_id — passing the WRONG org_id must not
    silently write into another org's location."""
    from app.repositories.delivery_repo import (
        db_get_location_delivery_config,
        db_set_location_delivery_config,
    )

    org_a = await _seed_org(db_conn, "Owner Org")
    org_b = await _seed_org(db_conn, "Intruder Org")
    location_id = await _seed_location(db_conn, org_a)

    with tenant_scope(org_b):
        result = await db_set_location_delivery_config(org_b, location_id, {"delivery_fee": 999})

    assert result is None

    with tenant_scope(org_a):
        untouched = await db_get_location_delivery_config(org_a, location_id)
    assert untouched == {}


# ── Public slug / public code lookups ───────────────────────────────────


async def test_get_org_by_slug_resolves_correct_org(db_conn):
    from app.repositories.delivery_repo import db_get_org_by_slug

    org_id = await _seed_org(db_conn, "Slug Lookup Org")
    slug = await db_conn.fetchval("SELECT slug FROM organizations WHERE id = $1", org_id)

    found = await db_get_org_by_slug(slug)
    assert found is not None
    assert found["id"] == org_id
    assert found["slug"] == slug

    assert await db_get_org_by_slug("this-slug-does-not-exist-xyz") is None


async def test_claim_public_code_survives_forced_collision(db_conn, monkeypatch):
    from app.repositories import delivery_repo

    org_id = await _seed_org(db_conn, "Code Collision Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        await _seed_order(db_conn, org_id=org_id, location_id=location_id, public_code="TAKEN1")
        target = await _seed_order(db_conn, org_id=org_id, location_id=location_id)

        calls = iter(["TAKEN1", "TAKEN1", "FRESH2"])
        monkeypatch.setattr(delivery_repo, "_random_code", lambda: next(calls))

        new_code = await delivery_repo.db_claim_public_code(target, org_id)

        stored = await db_conn.fetchval(
            "SELECT public_code FROM orders WHERE id = $1", target,
        )

    assert new_code == "FRESH2", "must retry past the colliding code and land on the free one"
    assert stored == "FRESH2", "the claimed code must actually be persisted on the order"


async def test_public_code_is_globally_unique_across_orgs(db_conn, monkeypatch):
    """The customer's /pedido/{code} URL carries no org_id, so a code must
    identify exactly ONE order in the whole database. If two tenants could
    mint the same code, db_get_order_by_public_code() would hand a customer
    another restaurant's order — the same bug class as the removed
    db_get_restaurant_by_id (see memory: ambiguous-restaurant-lookup-p0).
    """
    from app.repositories import delivery_repo

    org_a = await _seed_org(db_conn, "Global Code Org A")
    loc_a = await _seed_location(db_conn, org_a)
    org_b = await _seed_org(db_conn, "Global Code Org B")
    loc_b = await _seed_location(db_conn, org_b)

    with tenant_scope(org_a):
        await _seed_order(
            db_conn, org_id=org_a, location_id=loc_a, public_code="SHARE7",
        )

    # The database itself refuses the duplicate, even from another tenant.
    # Wrapped in a savepoint so the failed statement does not abort the
    # fixture's outer transaction.
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with db_conn.transaction():
            with tenant_scope(org_b):
                await _seed_order(
                    db_conn, org_id=org_b, location_id=loc_b, public_code="SHARE7",
                )

    # And claiming a code retries past the OTHER tenant's code instead of
    # blowing up, even though RLS makes that row invisible to this tenant.
    with tenant_scope(org_b):
        target = await _seed_order(db_conn, org_id=org_b, location_id=loc_b)
        calls = iter(["SHARE7", "OTHER8"])
        monkeypatch.setattr(delivery_repo, "_random_code", lambda: next(calls))
        assert await delivery_repo.db_claim_public_code(target, org_b) == "OTHER8"


async def test_generate_public_code_alphabet_is_unambiguous():
    from app.repositories.delivery_repo import _CODE_ALPHABET

    for banned in ("O", "0", "I", "1"):
        assert banned not in _CODE_ALPHABET


async def test_get_order_by_public_code_finds_the_right_order(db_conn):
    from app.repositories.delivery_repo import db_get_order_by_public_code

    org_id = await _seed_org(db_conn, "Public Code Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id, public_code="ABCD23",
        )

    found = await db_get_order_by_public_code("ABCD23")
    assert found is not None
    assert found["id"] == order_id
    assert found["org_id"] == org_id

    assert await db_get_order_by_public_code("NOPE00") is None


# ── Status transitions ───────────────────────────────────────────────────


async def test_accept_order_sets_fields_and_transitions(db_conn):
    from app.repositories.delivery_repo import db_accept_order

    org_id = await _seed_org(db_conn, "Accept Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(db_conn, org_id=org_id, location_id=location_id)
        staff_id = await _seed_staff(db_conn, org_id, location_id)

        result = await db_accept_order(org_id, order_id, staff_id, eta_minutes=25)

    assert result is not None
    assert result["status"] == "en_preparacion"
    assert result["accepted_by_staff_id"] == uuid.UUID(staff_id)
    assert result["estimated_minutes"] == 25
    assert _recent_utc(result["accepted_at"])


async def test_accept_order_refused_when_not_pending(db_conn):
    from app.repositories.delivery_repo import db_accept_order

    org_id = await _seed_org(db_conn, "Already Accepted Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id, status="en_preparacion",
        )
        staff_id = await _seed_staff(db_conn, org_id, location_id)

        result = await db_accept_order(org_id, order_id, staff_id, eta_minutes=15)

    assert result is None
    row = await _fetch_order(db_conn, order_id)
    assert row["status"] == "en_preparacion"
    assert row["accepted_at"] is None


async def test_reject_order_sets_reason(db_conn):
    from app.repositories.delivery_repo import db_reject_order

    org_id = await _seed_org(db_conn, "Reject Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(db_conn, org_id=org_id, location_id=location_id)
        result = await db_reject_order(org_id, order_id, "Sin repartidores disponibles")

    assert result is not None
    assert result["status"] == "rechazado"
    assert result["rejection_reason"] == "Sin repartidores disponibles"
    assert _recent_utc(result["rejected_at"])


async def test_cancel_order_allowed_before_acceptance(db_conn):
    from app.repositories.delivery_repo import db_cancel_order

    org_id = await _seed_org(db_conn, "Cancel Before Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(db_conn, org_id=org_id, location_id=location_id)
        result = await db_cancel_order(org_id, order_id)

    assert result is not None
    assert result["status"] == "cancelado"
    assert _recent_utc(result["cancelled_at"])


async def test_cancel_order_refused_after_acceptance(db_conn):
    """The customer can cancel ONLY until acceptance — the SQL WHERE must
    refuse it afterward, not just the Python caller's discipline."""
    from app.repositories.delivery_repo import db_accept_order, db_cancel_order

    org_id = await _seed_org(db_conn, "Cancel After Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(db_conn, org_id=org_id, location_id=location_id)
        staff_id = await _seed_staff(db_conn, org_id, location_id)
        accepted = await db_accept_order(org_id, order_id, staff_id, eta_minutes=20)
        assert accepted is not None

        result = await db_cancel_order(org_id, order_id)

    assert result is None, "cancel must be refused once the order has been accepted"
    row = await _fetch_order(db_conn, order_id)
    assert row["status"] == "en_preparacion"
    assert row["cancelled_at"] is None


async def test_assign_courier_and_mark_delivered_happy_path(db_conn):
    from app.repositories.delivery_repo import db_assign_courier, db_mark_delivered

    org_id = await _seed_org(db_conn, "Courier Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id, status="en_camino",
        )
        courier_id = await _seed_staff(db_conn, org_id, location_id, role="domiciliario")

        assigned = await db_assign_courier(org_id, order_id, courier_id)
        assert assigned is not None
        assert assigned["courier_staff_id"] == uuid.UUID(courier_id)
        assert _recent_utc(assigned["courier_assigned_at"])

        delivered = await db_mark_delivered(org_id, order_id)

    assert delivered is not None
    assert delivered["status"] == "entregado"
    assert _recent_utc(delivered["delivered_at"])


async def test_assign_courier_refused_on_terminal_status(db_conn):
    from app.repositories.delivery_repo import db_assign_courier

    org_id = await _seed_org(db_conn, "Terminal Status Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id, status="entregado",
        )
        courier_id = await _seed_staff(db_conn, org_id, location_id, role="domiciliario")

        result = await db_assign_courier(org_id, order_id, courier_id)

    assert result is None


# ── Listing ──────────────────────────────────────────────────────────────


async def test_list_delivery_orders_filters_by_location_and_status(db_conn):
    from app.repositories.delivery_repo import db_list_delivery_orders

    org_id = await _seed_org(db_conn, "Listing Org")
    loc_1 = await _seed_location(db_conn, org_id, "Sede 1")
    loc_2 = await _seed_location(db_conn, org_id, "Sede 2")

    with tenant_scope(org_id):
        order_1a = await _seed_order(db_conn, org_id=org_id, location_id=loc_1, status="pendiente_aceptacion")
        order_1b = await _seed_order(db_conn, org_id=org_id, location_id=loc_1, status="en_preparacion")
        await _seed_order(db_conn, org_id=org_id, location_id=loc_2, status="pendiente_aceptacion")

        all_loc1 = await db_list_delivery_orders(org_id, loc_1)
        pending_loc1 = await db_list_delivery_orders(org_id, loc_1, statuses=["pendiente_aceptacion"])

    all_loc1_ids = {o["id"] for o in all_loc1}
    assert all_loc1_ids == {order_1a, order_1b}

    pending_ids = {o["id"] for o in pending_loc1}
    assert pending_ids == {order_1a}


# ── Tenant isolation with a DELIBERATE id collision ─────────────────────


@pytest.fixture
async def collision(db_conn):
    """org_b's own id is forced to equal a location that actually belongs
    to org_a — same shape as test_p0_collision_regression.py."""
    org_a = await _seed_org(db_conn, "Collision Org A")
    org_b = await _seed_org(db_conn, "Collision Org B")

    loc_l = await _seed_location(db_conn, org_a, "Sede A (id collides with org B)", loc_id=org_b)
    assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"
    loc_b = await _seed_location(db_conn, org_b, "Sede B Principal")

    return {"org_a": org_a, "loc_l": loc_l, "org_b": org_b, "loc_b": loc_b}


async def test_tenant_isolation_list_never_leaks_other_org_or_sede(db_conn, collision):
    from app.repositories.delivery_repo import db_list_delivery_orders

    with tenant_scope(collision["org_a"]):
        order_a = await _seed_order(db_conn, org_id=collision["org_a"], location_id=collision["loc_l"])
    with tenant_scope(collision["org_b"]):
        order_b = await _seed_order(db_conn, org_id=collision["org_b"], location_id=collision["loc_b"])

    with tenant_scope(collision["org_a"]):
        result_a = await db_list_delivery_orders(collision["org_a"], collision["loc_l"])
    with tenant_scope(collision["org_b"]):
        result_b = await db_list_delivery_orders(collision["org_b"], collision["loc_b"])
        # loc_l's numeric id equals org_b's id, but loc_l does NOT belong to
        # org_b — asking org_b for that (org, location) pair must be empty.
        result_b_wrong_sede = await db_list_delivery_orders(collision["org_b"], collision["loc_l"])

    assert {o["id"] for o in result_a} == {order_a}
    assert {o["id"] for o in result_b} == {order_b}
    assert result_b_wrong_sede == [], (
        "org B must never see org A's orders even though loc_l's id equals org B's own id"
    )


async def test_tenant_isolation_cannot_transition_other_orgs_order(db_conn, collision):
    from app.repositories.delivery_repo import db_accept_order

    with tenant_scope(collision["org_b"]):
        order_b = await _seed_order(db_conn, org_id=collision["org_b"], location_id=collision["loc_b"])

    with tenant_scope(collision["org_a"]):
        staff_a = await _seed_staff(db_conn, collision["org_a"])
        # org_a tries to accept an order that actually belongs to org_b.
        result = await db_accept_order(collision["org_a"], order_b, staff_a, eta_minutes=10)

    assert result is None, "org A must never be able to transition org B's order"

    # RLS still applies to this raw connection — read back as org_b's own
    # scope, not org_a's (which would correctly see nothing).
    await _set_scope(db_conn, collision["org_b"])
    row = await _fetch_order(db_conn, order_b)
    assert row["status"] == "pendiente_aceptacion"
    assert row["accepted_at"] is None


# ── Closing a pickup order (no rider, never en_camino) ───────────────────


async def test_pickup_order_can_be_marked_collected_without_a_rider(db_conn):
    """A pickup ('recoger') order never goes en_camino — there is no rider.
    If db_mark_delivered only accepted en_camino/en_puerta, every pickup order
    would stay open forever and keep counting against the per-phone
    open-orders cap at checkout, eventually locking the customer out."""
    from app.repositories.delivery_repo import db_mark_delivered

    org_id = await _seed_org(db_conn, "Pickup Close Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        from_ready = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id,
            status="listo", order_type="recoger",
        )
        from_prep = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id,
            status="en_preparacion", order_type="recoger",
        )

        closed_ready = await db_mark_delivered(org_id, from_ready, location_id=location_id)
        closed_prep = await db_mark_delivered(org_id, from_prep, location_id=location_id)

        ready_row = await _fetch_order(db_conn, from_ready)
        prep_row = await _fetch_order(db_conn, from_prep)

    assert closed_ready is not None and closed_prep is not None
    assert ready_row["status"] == "entregado"
    assert prep_row["status"] == "entregado"
    assert ready_row["delivered_at"] is not None
    assert prep_row["delivered_at"] is not None


async def test_delivery_order_still_requires_a_rider_before_delivered(db_conn):
    """The pickup relaxation must NOT leak into delivery: a 'domicilio' order
    still in the kitchen cannot jump straight to entregado."""
    from app.repositories.delivery_repo import db_mark_delivered

    org_id = await _seed_org(db_conn, "Delivery Strict Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        order_id = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id,
            status="listo", order_type="domicilio",
        )
        result = await db_mark_delivered(org_id, order_id, location_id=location_id)
        row = await _fetch_order(db_conn, order_id)

    assert result is None, "a delivery order must leave with a rider before it is delivered"
    assert row["status"] == "listo"
    assert row["delivered_at"] is None


async def test_pickup_cannot_be_closed_before_the_cashier_accepts(db_conn):
    """Collecting is only possible once the kitchen has it: a pickup order
    still pendiente_aceptacion (or rejected) cannot be marked collected."""
    from app.repositories.delivery_repo import db_mark_delivered

    org_id = await _seed_org(db_conn, "Pickup Pending Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        pending = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id,
            status="pendiente_aceptacion", order_type="recoger",
        )
        rejected = await _seed_order(
            db_conn, org_id=org_id, location_id=location_id,
            status="rechazado", order_type="recoger",
        )
        assert await db_mark_delivered(org_id, pending, location_id=location_id) is None
        assert await db_mark_delivered(org_id, rejected, location_id=location_id) is None
        assert (await _fetch_order(db_conn, pending))["status"] == "pendiente_aceptacion"
        assert (await _fetch_order(db_conn, rejected))["status"] == "rechazado"
