"""
tests/test_tables_repo_tenant.py

Unit tests verifying that tables_repo functions correctly enforce tenant
isolation via tenant_connection().  No live database required — the asyncpg
pool is mocked at the module boundary.

Tests:
  A — tenant_scope set  → set_config GUC call is made with the correct id.
  B — no tenant context → TenantNotSetError raised BEFORE pool.acquire().
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant_context import (
    TenantNotSetError,
    tenant_scope,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn():
    """Build a minimal asyncpg connection mock with transaction() support."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
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


# ── Test A — tenant_scope activates set_config ────────────────────────────────

async def test_a_read_fn_calls_set_config_with_tenant_id():
    """
    db_get_restaurant_tables (mapped to db_get_tables) inside tenant_scope(5) must
    trigger SELECT set_config('app.restaurant_id', '5', true) on the connection
    before executing any application SQL.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(5):
            from app.repositories.tables_repo import db_get_tables
            await db_get_tables(branch_id=5)

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1, (
        f"Expected exactly one set_config call, got {set_config_calls}"
    )
    assert set_config_calls[0].args[1] == "5", (
        f"Expected tenant_id '5', got {set_config_calls[0].args[1]!r}"
    )


async def test_a_write_fn_calls_set_config_with_tenant_id():
    """
    db_get_active_session inside tenant_scope(12) must trigger set_config with '12'.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(12):
            from app.repositories.tables_repo import db_get_active_session
            await db_get_active_session("573001234567", "57300")

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1
    assert set_config_calls[0].args[1] == "12"


# ── Test B — no tenant context → TenantNotSetError before pool.acquire ────────

async def test_b_no_scope_raises_before_pool_acquire():
    """
    When no tenant_scope or bypass is active, TenantNotSetError must be raised
    immediately — pool.acquire must NOT be called.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(TenantNotSetError):
            from app.repositories.tables_repo import db_get_tables
            await db_get_tables(branch_id=1)

    pool.acquire.assert_not_called()


async def test_b_no_scope_write_fn_raises_before_pool_acquire():
    """Same check for a write-path function (db_create_table_session)."""
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(TenantNotSetError):
            from app.repositories.tables_repo import db_create_table_session
            await db_create_table_session("573001234567", "57300", "table-1-1", "Mesa 1")

    pool.acquire.assert_not_called()
