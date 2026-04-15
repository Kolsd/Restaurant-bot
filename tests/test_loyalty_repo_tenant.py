"""
tests/test_loyalty_repo_tenant.py

Unit tests verifying that loyalty_repo functions correctly enforce tenant
isolation via tenant_connection().  No live database required — the asyncpg
pool is mocked at the module boundary.

Tests:
  A — tenant_scope set  → set_config GUC call is made with the correct id.
  B — no tenant context → TenantNotSetError raised BEFORE pool.acquire().
  C — bypass_tenant_scope → SET LOCAL ROLE mesio_superadmin is executed.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.services.tenant_context import (
    TenantNotSetError,
    bypass_tenant_scope,
    tenant_scope,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn():
    """Build a minimal asyncpg connection mock."""
    conn = AsyncMock()
    # fetchrow used by _loyalty_cfg and db_get_loyalty_balance
    conn.fetchrow = AsyncMock(return_value=None)
    # fetchval used by set_config and idempotency checks
    conn.fetchval = AsyncMock(return_value=None)
    # fetch used by db_get_loyalty_ledger / db_get_loyalty_stats
    conn.fetch = AsyncMock(return_value=[])
    # execute used by INSERT / UPDATE statements
    conn.execute = AsyncMock(return_value=None)
    # transaction() returns an async context manager (savepoint — not used in
    # migrated code but kept for safety)
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
    db_get_loyalty_balance inside tenant_scope(42) must trigger
    SELECT set_config('app.restaurant_id', '42', true) on the connection
    before executing any application SQL.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(42):
            from app.repositories.loyalty_repo import db_get_loyalty_balance
            await db_get_loyalty_balance(42, "3001234567")

    # Verify set_config was called with '42' as the tenant string
    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1, (
        f"Expected exactly one set_config call, got {set_config_calls}"
    )
    assert set_config_calls[0].args[1] == "42", (
        f"Expected tenant_id '42', got {set_config_calls[0].args[1]!r}"
    )


async def test_a_write_fn_calls_set_config_with_tenant_id():
    """
    db_get_loyalty_ledger (a read that uses conn.fetch) inside tenant_scope(7)
    must trigger set_config with '7'.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(7):
            from app.repositories.loyalty_repo import db_get_loyalty_ledger
            await db_get_loyalty_ledger(7, "3001234567")

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1
    assert set_config_calls[0].args[1] == "7"


# ── Test B — no tenant context → TenantNotSetError before pool.acquire ────────

async def test_b_no_scope_raises_before_pool_acquire():
    """
    When no tenant_scope or bypass is active, TenantNotSetError must be raised
    immediately — pool.acquire must NOT be called.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        # Ensure no tenant is active (default state after module import)
        with pytest.raises(TenantNotSetError):
            from app.repositories.loyalty_repo import db_get_loyalty_balance
            await db_get_loyalty_balance(1, "3001234567")

    pool.acquire.assert_not_called()


async def test_b_no_scope_write_fn_raises_before_pool_acquire():
    """Same check for a write-path function (db_get_loyalty_stats)."""
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(TenantNotSetError):
            from app.repositories.loyalty_repo import db_get_loyalty_stats
            await db_get_loyalty_stats(1)

    pool.acquire.assert_not_called()


# ── Test C — bypass_tenant_scope → SET LOCAL ROLE mesio_superadmin ────────────

async def test_c_bypass_executes_set_local_role():
    """
    Inside bypass_tenant_scope, tenant_connection() must execute
    SET LOCAL ROLE mesio_superadmin instead of set_config.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with bypass_tenant_scope("pilot_integration_test"):
            from app.repositories.loyalty_repo import db_get_loyalty_balance
            await db_get_loyalty_balance(1, "3001234567")

    # conn.execute should have been called with SET LOCAL ROLE mesio_superadmin
    role_calls = [
        c for c in conn.execute.call_args_list
        if c.args and "SET LOCAL ROLE mesio_superadmin" in str(c.args[0])
    ]
    assert len(role_calls) == 1, (
        f"Expected SET LOCAL ROLE call, got execute calls: {conn.execute.call_args_list}"
    )

    # set_config must NOT have been called
    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 0, (
        f"set_config should not be called during bypass, got: {set_config_calls}"
    )
