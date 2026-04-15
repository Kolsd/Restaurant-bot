"""
tests/test_restaurant_repo_tenant.py

Unit tests verifying that restaurant_repo correctly enforces tenant isolation
via tenant_connection() for tenant-scoped functions, and that GLOBAL functions
continue to use the raw pool path (no set_config call).

Tests:
  1 — Tenant-scoped function inside tenant_scope() → set_config GUC called.
  2 — Tenant-scoped function without any scope → TenantNotSetError raised.
  3 — GLOBAL function (db_get_restaurant_by_phone) → set_config NOT called.
  4 — GLOBAL function inside bypass_tenant_scope → still works (pool path).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant_context import (
    TenantNotSetError,
    bypass_tenant_scope,
    tenant_scope,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn():
    """Build a minimal asyncpg connection mock."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool(conn):
    """Wrap conn in a minimal pool with acquire() as async ctx manager."""
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


# ── Test 1 — tenant_scope activates set_config ────────────────────────────────

@pytest.mark.asyncio
async def test_1_tenant_scoped_fn_calls_set_config():
    """
    db_save_restaurant_settings inside tenant_scope(5) must trigger
    SELECT set_config('app.restaurant_id', '5', true) on the connection.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(5):
            from app.repositories.restaurant_repo import db_save_restaurant_settings
            await db_save_restaurant_settings(
                restaurant_id=5,
                features={"timezone": "America/Bogota"},
            )

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1, (
        f"Expected exactly one set_config call, got: {set_config_calls}"
    )
    assert set_config_calls[0].args[1] == "5", (
        f"Expected tenant_id '5', got {set_config_calls[0].args[1]!r}"
    )


# ── Test 2 — no tenant context → TenantNotSetError before pool.acquire ────────

@pytest.mark.asyncio
async def test_2_no_scope_raises_tenant_not_set_error():
    """
    db_save_restaurant_settings without any tenant context must raise
    TenantNotSetError immediately — pool.acquire must NOT be called.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(TenantNotSetError):
            from app.repositories.restaurant_repo import db_save_restaurant_settings
            await db_save_restaurant_settings(
                restaurant_id=5,
                features={"timezone": "America/Bogota"},
            )

    # Pool must not have been acquired — error raised before any DB call
    pool.acquire.assert_not_called()


# ── Test 3 — GLOBAL function does NOT call set_config ────────────────────────

@pytest.mark.asyncio
async def test_3_global_fn_does_not_call_set_config():
    """
    db_get_restaurant_by_phone is a GLOBAL function (called from inbox_worker
    dispatch resolution before any tenant is pinned).  It must use _get_pool
    directly and must NOT call set_config, even when called outside any scope.
    """
    conn = _make_conn()
    # fetchrow returns None (restaurant not found) — that's fine for this test
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _make_pool(conn)

    # No tenant_scope active — GLOBAL functions must work without one
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_get_restaurant_by_phone
        result = await db_get_restaurant_by_phone("+573001234567")

    assert result is None  # not found — expected

    # set_config must NOT have been called (pool path, no tenant_connection)
    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 0, (
        f"GLOBAL function must not call set_config, got: {set_config_calls}"
    )


# ── Test 4 — GLOBAL function inside bypass_tenant_scope still works ───────────

@pytest.mark.asyncio
async def test_4_global_fn_works_inside_bypass_scope():
    """
    db_get_restaurant_by_phone must still work correctly when called inside
    bypass_tenant_scope (e.g. superadmin tooling).  It uses _get_pool, not
    tenant_connection, so bypass has no effect on it — it just succeeds.
    """
    conn = _make_conn()
    expected_row = {
        "id": 7,
        "name": "Restaurante Demo",
        "whatsapp_number": "573001234567",
        "wa_access_token": "tok",
        "wa_phone_id": "pid",
        "features": {},
        "menu": {},
        "address": "",
        "latitude": None,
        "longitude": None,
        "subscription_status": "active",
        "parent_restaurant_id": None,
        "plan": "basic",
        "marketing_enabled": True,
        "owner_phone": None,
        "timezone": "America/Bogota",
        "slug": None,
        "created_at": None,
        "updated_at": None,
    }
    conn.fetchrow = AsyncMock(return_value=expected_row)
    pool = _make_pool(conn)

    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        with bypass_tenant_scope("superadmin_lookup_test"):
            from app.repositories.restaurant_repo import db_get_restaurant_by_phone
            result = await db_get_restaurant_by_phone("573001234567")

    # Result should be the serialized row (or at minimum not None)
    assert result is not None
    assert result["id"] == 7
