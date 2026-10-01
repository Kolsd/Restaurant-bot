"""menu_availability must be keyed per SEDE: (org_id, location_id, dish_name).

Its primary key was `dish_name` ALONE, while every writer upserts with
`ON CONFLICT (dish_name, org_id)`. Postgres rejects an ON CONFLICT target
that no unique constraint matches, so every one of those statements raised
— and the one inside the order transaction (stock of a linked ingredient
falling to its minimum marks the dish sold out) rolled back the whole order.
Even with the upsert fixed, a single-column key means two restaurants cannot
both have a dish called "Bandeja Paisa": the second insert collides with a
row RLS hides from it.

Migration 0091 added the sede to the key. Each sede is its own restaurant
(PM 2026-09-20), so running out of a dish at Sede Norte must not take it off
Sede Centro's menu — which is exactly what the org-wide key did, including
automatically whenever one sede's inventory hit its minimum.

These run against the real database — the previous coverage
(tests/test_stock_autohide.py) mocks the connection, which is why none of
this was ever visible.
"""
from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


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


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """One connection, one rolled-back transaction, acting as mesio_app so
    RLS is genuinely exercised (docs/claude/testing.md)."""
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        # Match the production pool's jsonb codec so repo code that passes
        # raw lists/dicts to $n::jsonb encodes exactly once.
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


async def _pin(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))


async def _seed_org(conn, name: str) -> int:
    return await conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        name, f"ma-{uuid.uuid4().hex[:10]}",
    )


async def _seed_sede(conn, org_id: int, name: str = "Sede") -> int:
    return await conn.fetchval(
        "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
        org_id, name,
    )


async def test_marking_a_dish_sold_out_persists(db_conn):
    from app.repositories.restaurant_repo import (
        db_get_menu_availability, db_set_dish_availability,
    )

    org = await _seed_org(db_conn, "Availability Org")
    sede = await _seed_sede(db_conn, org)
    with tenant_scope(org):
        await _pin(db_conn, org)
        await db_set_dish_availability(org, "Ajiaco", False, location_id=sede)
        assert (await db_get_menu_availability(org, sede))["Ajiaco"] is False

        # Upsert, not a second row: flipping it back updates in place.
        await db_set_dish_availability(org, "Ajiaco", True, location_id=sede)
        assert (await db_get_menu_availability(org, sede))["Ajiaco"] is True
        count = await db_conn.fetchval(
            "SELECT count(*) FROM menu_availability WHERE org_id = $1 AND dish_name = $2",
            org, "Ajiaco",
        )
    assert count == 1


async def test_sold_out_at_one_sede_does_not_touch_the_other(db_conn):
    """The whole point of migration 0091. Each sede is its own restaurant:
    Sede Norte running out of ajiaco says nothing about Sede Centro."""
    from app.repositories.restaurant_repo import (
        db_get_menu_availability, db_set_dish_availability,
    )

    org = await _seed_org(db_conn, "Two Sede Org")
    norte = await _seed_sede(db_conn, org, "Sede Norte")
    centro = await _seed_sede(db_conn, org, "Sede Centro")

    with tenant_scope(org):
        await _pin(db_conn, org)
        await db_set_dish_availability(org, "Ajiaco", True, location_id=centro)
        await db_set_dish_availability(org, "Ajiaco", False, location_id=norte)

        assert (await db_get_menu_availability(org, norte))["Ajiaco"] is False
        assert (await db_get_menu_availability(org, centro))["Ajiaco"] is True


async def test_a_sede_never_sees_another_sedes_sold_out_rows(db_conn):
    """A read is scoped to one sede, not merged across the org."""
    from app.repositories.restaurant_repo import (
        db_get_menu_availability, db_set_dish_availability,
    )

    org = await _seed_org(db_conn, "Scoped Read Org")
    a = await _seed_sede(db_conn, org, "A")
    b = await _seed_sede(db_conn, org, "B")

    with tenant_scope(org):
        await _pin(db_conn, org)
        await db_set_dish_availability(org, "Solo en A", False, location_id=a)
        assert "Solo en A" not in (await db_get_menu_availability(org, b))


async def test_two_restaurants_can_share_a_dish_name(db_conn):
    """Org B marking its own "Bandeja Paisa" must neither fail against org A's
    row (which RLS hides from B) nor change org A's availability."""
    from app.repositories.restaurant_repo import (
        db_get_menu_availability, db_set_dish_availability,
    )

    org_a = await _seed_org(db_conn, "Availability A")
    org_b = await _seed_org(db_conn, "Availability B")
    sede_a = await _seed_sede(db_conn, org_a)
    sede_b = await _seed_sede(db_conn, org_b)

    with tenant_scope(org_a):
        await _pin(db_conn, org_a)
        await db_set_dish_availability(org_a, "Bandeja Paisa", True, location_id=sede_a)
    with tenant_scope(org_b):
        await _pin(db_conn, org_b)
        await db_set_dish_availability(org_b, "Bandeja Paisa", False, location_id=sede_b)
        assert (await db_get_menu_availability(org_b, sede_b))["Bandeja Paisa"] is False
    with tenant_scope(org_a):
        await _pin(db_conn, org_a)
        assert (await db_get_menu_availability(org_a, sede_a))["Bandeja Paisa"] is True


async def test_selling_the_last_unit_marks_sold_out_without_killing_the_order(db_conn):
    """The order transaction deducts stock; when a linked ingredient hits its
    minimum the dish is marked sold out in the SAME transaction. That upsert
    raising used to abort the transaction — i.e. the last unit of any dish
    could never be sold."""
    from app.repositories.orders_repo import deduct_inventory_in_tx
    from app.repositories.restaurant_repo import db_get_menu_availability

    org = await _seed_org(db_conn, "Last Unit Org")
    sede = await _seed_sede(db_conn, org)
    with tenant_scope(org):
        await _pin(db_conn, org)
        inv_id = await db_conn.fetchval(
            """INSERT INTO inventory (name, current_stock, min_stock, linked_dishes,
                                      org_id, location_id)
               VALUES ($1, 1, 0, $2::jsonb, $3, $4) RETURNING id""",
            "Masa de arepa", ["Arepa"], org, sede,
        )

        async with db_conn.transaction():  # savepoint, like the order tx
            await deduct_inventory_in_tx(
                db_conn, org, [{"name": "Arepa", "quantity": 1}], location_id=sede,
            )

        stock = await db_conn.fetchval("SELECT current_stock FROM inventory WHERE id = $1", inv_id)
        availability = await db_get_menu_availability(org, sede)

    assert stock == 0
    assert availability.get("Arepa") is False, "the dish must be marked sold out"
