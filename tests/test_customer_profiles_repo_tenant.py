"""
tests/test_customer_profiles_repo_tenant.py

Unit tests verifying that customer_profiles_repo functions correctly enforce
tenant isolation via tenant_connection().  No live database required — the
asyncpg pool is mocked at the module boundary.

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
    get_profile inside tenant_scope(7) must trigger
    SELECT set_config('app.restaurant_id', '7', true) on the connection.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(7):
            from app.repositories.customer_profiles_repo import get_profile
            await get_profile(7, "3001234567")

    set_config_calls = [
        c for c in conn.fetchval.call_args_list
        if c.args and "set_config" in str(c.args[0])
    ]
    assert len(set_config_calls) == 1, (
        f"Expected exactly one set_config call, got {set_config_calls}"
    )
    assert set_config_calls[0].args[1] == "7", (
        f"Expected tenant_id '7', got {set_config_calls[0].args[1]!r}"
    )


async def test_a_upsert_fn_calls_set_config_with_tenant_id():
    """
    upsert_profile_from_message inside tenant_scope(12) must trigger set_config with '12'.
    """
    conn = _make_conn()
    pool = _make_pool(conn)

    # upsert_profile_from_message uses fetchrow — mock it to return a valid dict-like row
    row_data = {
        "id": 1, "restaurant_id": 12, "phone": "3001234567",
        "display_name": None, "preferences": {}, "last_order_summary": None,
        "total_orders": 0, "total_spent": 0, "first_seen": None, "last_seen": None,
    }
    mock_row = MagicMock()
    mock_row.__iter__ = lambda s: iter(row_data.items())
    mock_row.keys = lambda: row_data.keys()
    mock_row.__getitem__ = lambda s, k: row_data[k]
    mock_row.get = lambda k, default=None: row_data.get(k, default)
    # asyncpg returns dict() from row — dict(row) requires __iter__ yielding (k, v)
    conn.fetchrow = AsyncMock(return_value=mock_row)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(12):
            from app.repositories.customer_profiles_repo import upsert_profile_from_message
            await upsert_profile_from_message(12, "3001234567")

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
            from app.repositories.customer_profiles_repo import get_profile
            await get_profile(1, "3001234567")

    pool.acquire.assert_not_called()
