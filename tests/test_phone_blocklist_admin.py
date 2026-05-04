"""
tests/test_phone_blocklist_admin.py
====================================
Tests for the admin phone-blocklist endpoints + supporting repo helpers
shipped to give Mesio operators a UI to manually block/unblock abusive
phones (in addition to the automatic block fired by tables.py when a
mesero marks a table as ghost).

Two test groups:
  1. Repo-layer integration tests against TEST_DATABASE_URL using the
     _ConnProxy / _PoolShim fixture pattern from test_loyalty_redemption.py
     (validated reference). These cover list/remove correctness + tenant
     isolation.
  2. Endpoint unit tests using FastAPI TestClient with auth mocked, no DB.
     These cover input validation + role-based access control.

Skipped when TEST_DATABASE_URL is not set for the integration tests.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

# ───────────────────── Group 2: endpoint unit tests (no DB) ─────────────────


from fastapi.testclient import TestClient
from app.main import app


def _make_user(role: str = "owner") -> dict:
    return {
        "username": "owner_test",
        "restaurant_name": "Bloqueador SA",
        "branch_id": 99,
        "role": role,
        "password_hash": "$2b$12$placeholder",
    }


def _make_restaurant() -> dict:
    return {
        "id": 99,
        "org_id": 99,
        "location_id": 99,
        "name": "Bloqueador SA",
        "whatsapp_number": "573000000001",
        "address": "Calle 1",
        "features": {"bot_active": True, "currency": "COP"},
    }


def _patch_auth(monkeypatch, role: str = "owner"):
    """Patch token verification + restaurant/user lookups."""
    from app.services import database as db

    monkeypatch.setattr(
        "app.routes.deps.verify_token",
        AsyncMock(return_value="owner_test"),
    )
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=_make_user(role)))
    monkeypatch.setattr(
        db,
        "db_get_restaurant_by_id",
        AsyncMock(return_value=_make_restaurant()),
    )


def test_block_endpoint_validates_phone_format(monkeypatch):
    """phone with letters → 400, no DB write attempted."""
    _patch_auth(monkeypatch, role="owner")
    add_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.add_to_blocklist", add_mock
    )

    client = TestClient(app)
    resp = client.post(
        "/api/admin/blocklist",
        json={"phone": "abc12345", "reason": "spam", "hours": 24},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400
    assert "dígitos" in resp.json().get("detail", "").lower()
    add_mock.assert_not_called()


def test_block_endpoint_validates_phone_length(monkeypatch):
    """phone with too few digits → 400."""
    _patch_auth(monkeypatch, role="owner")
    add_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.add_to_blocklist", add_mock
    )

    client = TestClient(app)
    # 6 digits — too short
    resp = client.post(
        "/api/admin/blocklist",
        json={"phone": "123456", "reason": "spam", "hours": 24},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400
    add_mock.assert_not_called()


def test_block_endpoint_validates_hours_range_low(monkeypatch):
    """hours=0 → 400."""
    _patch_auth(monkeypatch, role="owner")
    add_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.add_to_blocklist", add_mock
    )

    client = TestClient(app)
    resp = client.post(
        "/api/admin/blocklist",
        json={"phone": "573001234567", "reason": "spam", "hours": 0},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400
    assert "hours" in resp.json().get("detail", "").lower()
    add_mock.assert_not_called()


def test_block_endpoint_validates_hours_range_high(monkeypatch):
    """hours=999 (>720) → 400."""
    _patch_auth(monkeypatch, role="owner")
    add_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.add_to_blocklist", add_mock
    )

    client = TestClient(app)
    resp = client.post(
        "/api/admin/blocklist",
        json={"phone": "573001234567", "reason": "spam", "hours": 999},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400
    add_mock.assert_not_called()


def test_block_endpoint_requires_admin_role(monkeypatch):
    """role='mesero' → 403, no DB write."""
    _patch_auth(monkeypatch, role="mesero")
    add_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.add_to_blocklist", add_mock
    )

    client = TestClient(app)
    resp = client.post(
        "/api/admin/blocklist",
        json={"phone": "573001234567", "reason": "spam", "hours": 24},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 403
    assert "owner" in resp.json().get("detail", "").lower()
    add_mock.assert_not_called()


def test_block_endpoint_happy_path(monkeypatch):
    """owner + valid phone + valid hours → 200, add_to_blocklist called once."""
    _patch_auth(monkeypatch, role="owner")
    add_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.add_to_blocklist", add_mock
    )

    client = TestClient(app)
    resp = client.post(
        "/api/admin/blocklist",
        json={"phone": "+57 300 123 4567", "reason": "spam masivo", "hours": 72},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    # phone should be normalized (no '+', no spaces)
    assert body["phone"] == "573001234567"
    assert body["hours"] == 72
    add_mock.assert_awaited_once()
    call_kwargs = add_mock.call_args[1]
    assert call_kwargs["phone"] == "573001234567"
    assert call_kwargs["org_id"] == 99
    assert call_kwargs["hours"] == 72
    assert call_kwargs["reason"] == "spam masivo"


def test_unblock_endpoint_404_when_not_blocked(monkeypatch):
    """unblock phone that was not blocked → 404."""
    _patch_auth(monkeypatch, role="owner")
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.remove_from_blocklist",
        AsyncMock(return_value=False),
    )

    client = TestClient(app)
    resp = client.delete(
        "/api/admin/blocklist/573001234567",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 404
    assert "no estaba" in resp.json().get("detail", "").lower()


def test_unblock_endpoint_requires_admin_role(monkeypatch):
    """role='caja' → 403."""
    _patch_auth(monkeypatch, role="caja")
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.remove_from_blocklist", remove_mock
    )

    client = TestClient(app)
    resp = client.delete(
        "/api/admin/blocklist/573001234567",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 403
    remove_mock.assert_not_called()


def test_unblock_endpoint_happy_path(monkeypatch):
    """owner + valid phone → 200 success."""
    _patch_auth(monkeypatch, role="owner")
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.remove_from_blocklist", remove_mock
    )

    client = TestClient(app)
    resp = client.delete(
        "/api/admin/blocklist/573001234567",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["phone"] == "573001234567"
    remove_mock.assert_awaited_once_with("573001234567", 99)


def test_list_endpoint_returns_items(monkeypatch):
    """GET /api/admin/blocklist returns the items list."""
    _patch_auth(monkeypatch, role="owner")
    fake_items = [
        {
            "id": 1,
            "phone": "573001234567",
            "phone_obf": "***4567",
            "reason": "spam",
            "blocked_by": "owner_test",
            "blocked_until": "2099-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    monkeypatch.setattr(
        "app.repositories.phone_blocklist_repo.list_active_blocks_for_org",
        AsyncMock(return_value=fake_items),
    )

    client = TestClient(app)
    resp = client.get(
        "/api/admin/blocklist",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["phone_obf"] == "***4567"
    assert data["items"][0]["reason"] == "spam"


# ───────────────────── Group 1: repo integration tests (DB) ─────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")


# Helpers borrowed from test_loyalty_redemption.py — proven fixture pattern.

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
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set — repo integration tests skipped")
    import asyncpg
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back connection with get_pool mocked to that single conn."""
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
    """Create one test org and return its id."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "PhoneBlocklistTestOrg",
        f"phone-block-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org),
    )


@pytest.mark.asyncio
async def test_list_active_blocks_returns_only_unexpired(db_conn, org_id):
    """Insert 2 blocks (one expired, one active) → list returns only the active one."""
    from app.repositories.phone_blocklist_repo import list_active_blocks_for_org
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)

    # Active block: 24h in the future.
    await db_conn.execute(
        """INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
           VALUES ($1, $2, $3, $4, NOW() + INTERVAL '24 hours')""",
        "573001111111", org_id, "active block", "owner_test",
    )
    # Expired block: 1h in the past.
    await db_conn.execute(
        """INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
           VALUES ($1, $2, $3, $4, NOW() - INTERVAL '1 hour')""",
        "573002222222", org_id, "expired block", "owner_test",
    )

    with tenant_scope(org_id):
        items = await list_active_blocks_for_org(org_id, limit=100)

    phones_returned = {it["phone"] for it in items}
    assert "573001111111" in phones_returned
    assert "573002222222" not in phones_returned
    # Verify the obfuscated form
    active = next(it for it in items if it["phone"] == "573001111111")
    assert active["phone_obf"] == "***1111"
    assert active["reason"] == "active block"
    assert active["blocked_by"] == "owner_test"
    assert active["blocked_until"] is not None


@pytest.mark.asyncio
async def test_list_active_blocks_tenant_isolated(db_conn, org_id):
    """Org A blocks → org B's query does NOT see them (RLS + explicit org_id filter)."""
    from app.repositories.phone_blocklist_repo import list_active_blocks_for_org
    from app.services.tenant_context import tenant_scope

    # Create a second org.
    org_b = await db_conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "PhoneBlocklistTestOrg-B",
        f"phone-block-b-{uuid.uuid4().hex[:8]}",
    )

    # Org A inserts a block.
    await _set_scope(db_conn, org_id)
    await db_conn.execute(
        """INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
           VALUES ($1, $2, $3, $4, NOW() + INTERVAL '24 hours')""",
        "573009999999", org_id, "org A block", "owner_a",
    )

    # Org B looks up — should see nothing for itself, but it must NOT see org A's row.
    await _set_scope(db_conn, org_b)
    with tenant_scope(org_b):
        items_b = await list_active_blocks_for_org(org_b, limit=100)
    assert all(it["phone"] != "573009999999" for it in items_b)

    # Sanity: org A still sees its own row.
    await _set_scope(db_conn, org_id)
    with tenant_scope(org_id):
        items_a = await list_active_blocks_for_org(org_id, limit=100)
    assert any(it["phone"] == "573009999999" for it in items_a)


@pytest.mark.asyncio
async def test_remove_from_blocklist_returns_true_on_match(db_conn, org_id):
    """Insert + remove → returns True, row gone from active list."""
    from app.repositories.phone_blocklist_repo import (
        list_active_blocks_for_org,
        remove_from_blocklist,
    )
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    await db_conn.execute(
        """INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
           VALUES ($1, $2, $3, $4, NOW() + INTERVAL '24 hours')""",
        "573003333333", org_id, "to be removed", "owner_test",
    )

    with tenant_scope(org_id):
        result = await remove_from_blocklist("573003333333", org_id)
        assert result is True

        # Confirm it no longer appears in the active list.
        items = await list_active_blocks_for_org(org_id, limit=100)
        assert all(it["phone"] != "573003333333" for it in items)


@pytest.mark.asyncio
async def test_remove_from_blocklist_returns_false_when_no_match(db_conn, org_id):
    """remove on a phone that was never blocked → returns False, no error."""
    from app.repositories.phone_blocklist_repo import remove_from_blocklist
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    with tenant_scope(org_id):
        result = await remove_from_blocklist("573009000000", org_id)
        assert result is False


@pytest.mark.asyncio
async def test_remove_from_blocklist_tenant_isolated(db_conn, org_id):
    """Org B cannot remove Org A's blocks — returns False for unmatched org."""
    from app.repositories.phone_blocklist_repo import remove_from_blocklist
    from app.services.tenant_context import tenant_scope

    # Create org B.
    org_b = await db_conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "PhoneBlocklistTestOrg-B-rm",
        f"phone-block-rm-b-{uuid.uuid4().hex[:8]}",
    )

    # Org A blocks a phone.
    await _set_scope(db_conn, org_id)
    await db_conn.execute(
        """INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
           VALUES ($1, $2, $3, $4, NOW() + INTERVAL '24 hours')""",
        "573004444444", org_id, "org A only", "owner_a",
    )

    # Org B tries to remove that phone — passing its OWN org_id. No match.
    await _set_scope(db_conn, org_b)
    with tenant_scope(org_b):
        result = await remove_from_blocklist("573004444444", org_b)
        assert result is False

    # Sanity: row still exists for org A.
    await _set_scope(db_conn, org_id)
    still_there = await db_conn.fetchval(
        "SELECT COUNT(*) FROM phone_blocklist WHERE phone=$1 AND org_id=$2",
        "573004444444", org_id,
    )
    assert still_there == 1
