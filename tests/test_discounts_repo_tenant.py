"""
tests/test_discounts_repo_tenant.py

Unit tests verifying that discounts_repo functions correctly enforce tenant
isolation via tenant_connection().  No live database required — the asyncpg
pool is mocked at the module boundary.

Tests:
  A — tenant_scope set  → set_config GUC call is made with the correct id.
  B — no tenant context → TenantNotSetError raised BEFORE pool.acquire().
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant_context import TenantNotSetError, tenant_scope


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn():
    """Build a minimal asyncpg connection mock."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="DELETE 0")
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
    db_get_all_discounts inside tenant_scope(10) must trigger
    SELECT set_config('app.restaurant_id', '10', true) on the connection.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(10):
            from app.repositories.discounts_repo import db_get_all_discounts
            await db_get_all_discounts(10)

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1, (
        f"Expected exactly one set_config call, got {set_config_calls}"
    )
    assert set_config_calls[0].args[1] == "10", (
        f"Expected tenant_id '10', got {set_config_calls[0].args[1]!r}"
    )


async def test_a_active_discount_calls_set_config():
    """
    db_get_active_discount inside tenant_scope(3) must trigger set_config with '3'.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(3):
            from app.repositories.discounts_repo import db_get_active_discount
            await db_get_active_discount(3)

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1
    assert set_config_calls[0].args[1] == "3"


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
            from app.repositories.discounts_repo import db_get_all_discounts
            await db_get_all_discounts(1)

    pool.acquire.assert_not_called()
