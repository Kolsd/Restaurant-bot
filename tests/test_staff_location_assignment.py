"""A staff member's sede (staff.location_id) is what scopes them to their own
sede's orders — the staff JWT carries it, and the cashier's Domicilios
section refuses anyone without one. Staff created through the app never got
one: staff_repo.db_create_staff did not write it and both creation routes
dropped the sede they had resolved. Real database, acting as mesio_app.
"""
from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from fastapi import HTTPException

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
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
        )
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


async def _seed_org(conn, n_locations: int) -> tuple[int, list[int]]:
    org = await conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        f"Staff Loc Org {uuid.uuid4().hex[:6]}", f"sl-{uuid.uuid4().hex[:10]}",
    )
    locs = []
    for i in range(n_locations):
        locs.append(await conn.fetchval(
            "INSERT INTO locations (org_id, name, active) VALUES ($1, $2, true) RETURNING id",
            org, f"Sede {i + 1}",
        ))
    return org, locs


async def test_single_sede_org_assigns_that_sede_without_asking(db_conn):
    from app.routes.staff import _resolve_new_staff_location

    org, (only,) = await _seed_org(db_conn, 1)
    with tenant_scope(org):
        assert await _resolve_new_staff_location(org, None) == only


async def test_multi_sede_org_never_guesses(db_conn):
    from app.routes.staff import _resolve_new_staff_location

    org, locs = await _seed_org(db_conn, 2)
    with tenant_scope(org):
        assert await _resolve_new_staff_location(org, None) is None
        assert await _resolve_new_staff_location(org, locs[1]) == locs[1]


async def test_a_sede_of_another_org_is_refused(db_conn):
    from app.routes.staff import _resolve_new_staff_location

    org_a, _ = await _seed_org(db_conn, 1)
    _, (foreign,) = await _seed_org(db_conn, 1)
    with tenant_scope(org_a):
        with pytest.raises(HTTPException) as exc:
            await _resolve_new_staff_location(org_a, foreign)
    assert exc.value.status_code == 403


async def test_db_create_staff_persists_the_sede(db_conn):
    from app.repositories.staff_repo import db_create_staff

    org, (loc,) = await _seed_org(db_conn, 1)
    with tenant_scope(org):
        await db_conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org))
        member = await db_create_staff(
            restaurant_id=org, name="Cajera Prueba", role="caja",
            pin_hash="x", roles=["caja"], username=f"cajera.{uuid.uuid4().hex[:6]}",
            location_id=loc,
        )
        stored = await db_conn.fetchval(
            "SELECT location_id FROM staff WHERE id = $1::uuid", member["id"],
        )
    assert member["location_id"] == loc
    assert stored == loc


async def test_branches_list_carries_each_sedes_own_name(db_conn):
    """The /locations page (GET /api/team/branches -> db_get_branches) showed
    the ORGANIZATION's name on every sede card: the legacy `restaurants` view
    exposes the org name as `name`. Each row must carry the sede's own name."""
    from app.repositories.restaurant_repo import db_get_branches

    org, locs = await _seed_org(db_conn, 2)
    with tenant_scope(org):
        rows = await db_get_branches(org)
    by_id = {int(r["id"]): r for r in rows}
    assert by_id[locs[0]]["location_name"] == "Sede 1"
    assert by_id[locs[1]]["location_name"] == "Sede 2"
