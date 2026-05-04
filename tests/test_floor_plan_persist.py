"""
tests/test_floor_plan_persist.py
=================================
Integration tests for the floor plan bulk-save pipeline:
  - db_save_floor_plan_bulk (repo)  — happy path, partial updates,
    invalid id, tenant isolation, capacity range guard.
  - POST /api/tables/floor-plan (route) — role gate, range validation,
    table_type whitelist.

Requirements:
  - TEST_DATABASE_URL must be set for the repo tests (otherwise skipped).
  - The route-level tests use FastAPI TestClient with dependency overrides
    and run regardless of TEST_DATABASE_URL because they exercise pure
    request validation (the repo call is mocked).

Reuses the _ConnProxy / _PoolShim pattern from
tests/test_loyalty_aggregates.py — that fixture is the validated
reference for integration tests post-Wave-2.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import app


# ── Skip marker for repo tests ───────────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_db_required = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers (mirror tests/test_loyalty_aggregates.py) ────────────────────────


class _ConnProxy:
    """Thin asyncpg-compatible proxy — works around Connection.__slots__."""

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


# ── Fixtures (only used by repo tests; gated by _db_required) ────────────────


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back connection with get_pool mocked."""
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


async def _set_scope(conn, org_id):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


@pytest.fixture
async def org_with_location(db_conn):
    """Create a fresh org + one location and return (org_id, location_id)."""
    suffix = uuid.uuid4().hex[:8]
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        f"FloorPlanTestOrg_{suffix}",
        f"floorplan-org-{suffix}",
    )
    org_id = row["id"]

    loc = await db_conn.fetchrow(
        """
        INSERT INTO locations (org_id, name, slug)
        VALUES ($1, $2, $3) RETURNING id
        """,
        org_id, "Sede principal", f"sede-{suffix}",
    )
    return org_id, loc["id"]


async def _seed_table(conn, org_id: int, location_id: int, **overrides) -> str:
    """Insert a restaurant_tables row and return its id."""
    tid = f"tbl-{uuid.uuid4().hex[:10]}"
    cols = {
        "id": tid,
        "number": overrides.get("number", 1),
        "name": overrides.get("name", "Mesa 1"),
        "branch_id": overrides.get("branch_id", location_id),
        "org_id": org_id,
        "location_id": location_id,
        "active": True,
        "capacity": overrides.get("capacity", 4),
        "table_type": overrides.get("table_type", "interior"),
        "zone": overrides.get("zone", ""),
        "position_x": overrides.get("position_x", 0),
        "position_y": overrides.get("position_y", 0),
    }
    await conn.execute(
        """
        INSERT INTO restaurant_tables
            (id, number, name, branch_id, org_id, location_id, active,
             capacity, table_type, zone, position_x, position_y)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        cols["id"], cols["number"], cols["name"], cols["branch_id"],
        cols["org_id"], cols["location_id"], cols["active"], cols["capacity"],
        cols["table_type"], cols["zone"], cols["position_x"], cols["position_y"],
    )
    return tid


# ══════════════════════════════════════════════════════════════════════════════
# Repo tests (require TEST_DATABASE_URL)
# ══════════════════════════════════════════════════════════════════════════════


@_db_required
@pytest.mark.asyncio
async def test_save_table_position_only(db_conn, org_with_location):
    """Updating only position_x/y leaves capacity / table_type / zone intact."""
    from app.repositories.tables_repo import db_save_floor_plan_bulk
    from app.services.tenant_context import tenant_scope

    org_id, loc_id = org_with_location
    await _set_scope(db_conn, org_id)
    tid = await _seed_table(
        db_conn, org_id, loc_id,
        capacity=6, table_type="terraza", zone="patio",
        position_x=10, position_y=20,
    )

    with tenant_scope(org_id):
        result = await db_save_floor_plan_bulk(
            org_id,
            [{"id": tid, "position_x": 100.5, "position_y": 200.25}],
        )

    assert result == {"updated": 1, "skipped": 0, "errors": []}

    row = await db_conn.fetchrow(
        "SELECT position_x, position_y, capacity, table_type, zone "
        "FROM restaurant_tables WHERE id = $1",
        tid,
    )
    assert row["position_x"] == pytest.approx(100.5)
    assert row["position_y"] == pytest.approx(200.25)
    # Untouched columns survived.
    assert row["capacity"] == 6
    assert row["table_type"] == "terraza"
    assert row["zone"] == "patio"


@_db_required
@pytest.mark.asyncio
async def test_save_bulk_updates_multiple(db_conn, org_with_location):
    """Three tables, all updated → updated=3, errors=[]."""
    from app.repositories.tables_repo import db_save_floor_plan_bulk
    from app.services.tenant_context import tenant_scope

    org_id, loc_id = org_with_location
    await _set_scope(db_conn, org_id)
    t1 = await _seed_table(db_conn, org_id, loc_id, number=1, name="M1")
    t2 = await _seed_table(db_conn, org_id, loc_id, number=2, name="M2")
    t3 = await _seed_table(db_conn, org_id, loc_id, number=3, name="M3")

    with tenant_scope(org_id):
        result = await db_save_floor_plan_bulk(
            org_id,
            [
                {"id": t1, "position_x": 1.0, "position_y": 2.0},
                {"id": t2, "capacity": 8},
                {"id": t3, "zone": "barra", "table_type": "barra"},
            ],
        )

    assert result["updated"] == 3
    assert result["skipped"] == 0
    assert result["errors"] == []


@_db_required
@pytest.mark.asyncio
async def test_save_bulk_skips_invalid_id(db_conn, org_with_location):
    """Entries without a valid id are reported in errors[] without crashing."""
    from app.repositories.tables_repo import db_save_floor_plan_bulk
    from app.services.tenant_context import tenant_scope

    org_id, loc_id = org_with_location
    await _set_scope(db_conn, org_id)
    good = await _seed_table(db_conn, org_id, loc_id)

    with tenant_scope(org_id):
        result = await db_save_floor_plan_bulk(
            org_id,
            [
                {"id": good, "position_x": 5.0, "position_y": 5.0},
                {"id": None, "position_x": 1.0},          # invalid id
                {"id": "", "capacity": 4},                # invalid (empty)
                {"id": "tbl-does-not-exist", "zone": "x"},  # not in DB
            ],
        )

    assert result["updated"] == 1
    # None and "" → invalid_id ; bogus id → not_found_or_wrong_org
    reasons = sorted(e["reason"] for e in result["errors"])
    assert reasons == ["invalid_id", "invalid_id", "not_found_or_wrong_org"]


@_db_required
@pytest.mark.asyncio
async def test_save_bulk_no_fields_skipped(db_conn, org_with_location):
    """Entry with id but no mutable fields lands in skipped, not updated."""
    from app.repositories.tables_repo import db_save_floor_plan_bulk
    from app.services.tenant_context import tenant_scope

    org_id, loc_id = org_with_location
    await _set_scope(db_conn, org_id)
    tid = await _seed_table(db_conn, org_id, loc_id)

    with tenant_scope(org_id):
        result = await db_save_floor_plan_bulk(org_id, [{"id": tid}])

    assert result == {"updated": 0, "skipped": 1, "errors": []}


@_db_required
@pytest.mark.asyncio
async def test_save_bulk_tenant_isolation(db_conn):
    """Org A can't update tables that belong to org B — surfaces in errors[]."""
    from app.repositories.tables_repo import db_save_floor_plan_bulk
    from app.services.tenant_context import tenant_scope

    suffix_a = uuid.uuid4().hex[:8]
    suffix_b = uuid.uuid4().hex[:8]
    org_a = await db_conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        f"FloorPlanIsolA_{suffix_a}", f"floorplan-iso-a-{suffix_a}",
    )
    org_b = await db_conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        f"FloorPlanIsolB_{suffix_b}", f"floorplan-iso-b-{suffix_b}",
    )
    loc_a = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, slug) VALUES ($1, $2, $3) RETURNING id",
        org_a, "A", f"loc-a-{suffix_a}",
    )
    loc_b = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, slug) VALUES ($1, $2, $3) RETURNING id",
        org_b, "B", f"loc-b-{suffix_b}",
    )

    # Seed a table in each org. We need to set the appropriate scope GUC
    # before each insert (FORCE RLS otherwise blocks WITH CHECK).
    await _set_scope(db_conn, org_a)
    tid_a = await _seed_table(db_conn, org_a, loc_a)

    await _set_scope(db_conn, org_b)
    tid_b = await _seed_table(db_conn, org_b, loc_b)

    # Re-scope to org A and try to update org B's table — should NOT update.
    await _set_scope(db_conn, org_a)
    with tenant_scope(org_a):
        result = await db_save_floor_plan_bulk(
            org_a,
            [
                {"id": tid_a, "position_x": 1.0},
                {"id": tid_b, "position_x": 999.0},  # cross-tenant attempt
            ],
        )

    assert result["updated"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["table_id"] == tid_b
    assert result["errors"][0]["reason"] == "not_found_or_wrong_org"

    # Verify org B's table wasn't mutated. We must drop into mesio_superadmin
    # to read across tenant scopes; do it by raising scope to org B.
    await _set_scope(db_conn, org_b)
    pos_x = await db_conn.fetchval(
        "SELECT position_x FROM restaurant_tables WHERE id = $1", tid_b,
    )
    assert pos_x == pytest.approx(0)  # default value, untouched


# ══════════════════════════════════════════════════════════════════════════════
# Route tests — exercise request validation; repo call mocked
# ══════════════════════════════════════════════════════════════════════════════


_RESTAURANT = {
    "id": 1,
    "org_id": 1,
    "location_id": 1,
    "name": "FloorPlanTest",
    "whatsapp_number": "+573000000000",
    "features": {},
}


def _override_admin():
    from contextlib import contextmanager  # noqa: F401  (kept for clarity)
    from app.services.tenant_context import tenant_scope

    async def _restaurant_dep(request=None):
        with tenant_scope(_RESTAURANT["id"]):
            yield _RESTAURANT

    async def _user_dep(request=None):
        return {"username": "admin@test", "role": "admin", "restaurant_id": _RESTAURANT["id"]}

    return _restaurant_dep, _user_dep


def _override_mesero():
    from app.services.tenant_context import tenant_scope

    async def _restaurant_dep(request=None):
        with tenant_scope(_RESTAURANT["id"]):
            yield _RESTAURANT

    async def _user_dep(request=None):
        return {"username": "mesero@test", "role": "mesero", "restaurant_id": _RESTAURANT["id"]}

    return _restaurant_dep, _user_dep


def _install_overrides(restaurant_dep, user_dep):
    from app.routes.deps import get_current_restaurant_scoped, get_current_user
    app.dependency_overrides[get_current_restaurant_scoped] = restaurant_dep
    app.dependency_overrides[get_current_user] = user_dep


def _clear_overrides():
    from app.routes.deps import get_current_restaurant_scoped, get_current_user
    app.dependency_overrides.pop(get_current_restaurant_scoped, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_save_endpoint_rejects_non_admin_role():
    """Mesero hitting POST /api/tables/floor-plan → 403."""
    rest_dep, user_dep = _override_mesero()
    _install_overrides(rest_dep, user_dep)
    try:
        with patch(
            "app.services.database.db_save_floor_plan_bulk",
            AsyncMock(return_value={"updated": 0, "skipped": 0, "errors": []}),
        ) as repo_mock:
            client = TestClient(app)
            resp = client.post(
                "/api/tables/floor-plan",
                json={"tables": [{"id": "t1", "position_x": 1.0}]},
                headers={"Authorization": "Bearer test_token"},
            )
        assert resp.status_code == 403
        # Repo must NOT have been touched when role gate denies.
        repo_mock.assert_not_awaited()
    finally:
        _clear_overrides()


def test_save_endpoint_position_out_of_range():
    """position_x > 10000 → 422 from Pydantic Field validator."""
    rest_dep, user_dep = _override_admin()
    _install_overrides(rest_dep, user_dep)
    try:
        with patch(
            "app.services.database.db_save_floor_plan_bulk",
            AsyncMock(return_value={"updated": 0, "skipped": 0, "errors": []}),
        ) as repo_mock:
            client = TestClient(app)
            resp = client.post(
                "/api/tables/floor-plan",
                json={"tables": [{"id": "t1", "position_x": 99999}]},
                headers={"Authorization": "Bearer test_token"},
            )
        # FastAPI returns 422 for body validation; our explicit checks return 400
        # or 403, but range guards live in Field(ge=, le=) → 422.
        assert resp.status_code == 422
        repo_mock.assert_not_awaited()
    finally:
        _clear_overrides()


def test_save_endpoint_capacity_validates_range():
    """capacity outside 1..100 → 422."""
    rest_dep, user_dep = _override_admin()
    _install_overrides(rest_dep, user_dep)
    try:
        client = TestClient(app)
        # capacity=0 (below min)
        with patch(
            "app.services.database.db_save_floor_plan_bulk",
            AsyncMock(return_value={"updated": 0, "skipped": 0, "errors": []}),
        ):
            resp_low = client.post(
                "/api/tables/floor-plan",
                json={"tables": [{"id": "t1", "capacity": 0}]},
                headers={"Authorization": "Bearer test_token"},
            )
        assert resp_low.status_code == 422

        # capacity=101 (above max)
        with patch(
            "app.services.database.db_save_floor_plan_bulk",
            AsyncMock(return_value={"updated": 0, "skipped": 0, "errors": []}),
        ):
            resp_hi = client.post(
                "/api/tables/floor-plan",
                json={"tables": [{"id": "t1", "capacity": 101}]},
                headers={"Authorization": "Bearer test_token"},
            )
        assert resp_hi.status_code == 422
    finally:
        _clear_overrides()


def test_save_endpoint_table_type_invalid_rejected():
    """table_type='circle' not in whitelist → 400 (explicit check) not 422."""
    rest_dep, user_dep = _override_admin()
    _install_overrides(rest_dep, user_dep)
    try:
        with patch(
            "app.services.database.db_save_floor_plan_bulk",
            AsyncMock(return_value={"updated": 0, "skipped": 0, "errors": []}),
        ) as repo_mock:
            client = TestClient(app)
            resp = client.post(
                "/api/tables/floor-plan",
                json={"tables": [{"id": "t1", "table_type": "circle"}]},
                headers={"Authorization": "Bearer test_token"},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert "table_type" in body["detail"]
        repo_mock.assert_not_awaited()
    finally:
        _clear_overrides()


def test_save_endpoint_admin_happy_path():
    """Admin POSTs valid body → 200, repo invoked once with org_id and payload."""
    rest_dep, user_dep = _override_admin()
    _install_overrides(rest_dep, user_dep)
    try:
        repo_response = {"updated": 2, "skipped": 0, "errors": []}
        with patch(
            "app.services.database.db_save_floor_plan_bulk",
            AsyncMock(return_value=repo_response),
        ) as repo_mock:
            client = TestClient(app)
            resp = client.post(
                "/api/tables/floor-plan",
                json={
                    "tables": [
                        {"id": "t1", "position_x": 10, "position_y": 20},
                        {"id": "t2", "capacity": 8, "table_type": "interior"},
                    ]
                },
                headers={"Authorization": "Bearer test_token"},
            )
        assert resp.status_code == 200
        assert resp.json() == repo_response
        repo_mock.assert_awaited_once()
        # Inspect the repo call args.
        kwargs = repo_mock.call_args.kwargs
        assert kwargs["org_id"] == _RESTAURANT["id"]
        assert len(kwargs["tables"]) == 2
        # exclude_none: {"id":"t1","position_x":10,"position_y":20} (no Nones)
        assert "capacity" not in kwargs["tables"][0]
    finally:
        _clear_overrides()
