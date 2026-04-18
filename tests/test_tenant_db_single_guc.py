"""
tests/test_tenant_db_single_guc.py

Unit tests verifying that tenant_connection() (Wave 2) sets ONLY app.org_id
and does NOT set the legacy app.restaurant_id GUC.

These tests use mocks — no real DB connection required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.services.tenant_context import tenant_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_conn():
    """Build a minimal async mock that mimics asyncpg.Connection for our tests.

    Key invariant: conn.transaction() must return a synchronous MagicMock that
    implements the async context manager protocol (__aenter__/__aexit__) via
    AsyncMock attributes, NOT a coroutine itself. AsyncMock's default for a
    method call is to return a coroutine, which does not have __aenter__.
    """
    conn = AsyncMock()
    # conn.execute() and conn.fetchval() are AsyncMocks (return awaitable None).

    # Build a sync MagicMock that acts as an async context manager.
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    # transaction() must be a regular (sync) callable that returns txn,
    # not an AsyncMock (which would return a coroutine, not a context manager).
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_mock_pool(conn):
    """Build a minimal mock pool whose .acquire() returns conn via __aenter__."""
    pool = MagicMock()
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire.return_value = acquire_ctx
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_connection_sets_only_org_guc():
    """Wave 2: tenant_connection() calls set_config('app.org_id', ...) once."""
    conn = _make_mock_conn()
    pool = _make_mock_pool(conn)

    with patch("app.services.database.get_pool", new=AsyncMock(return_value=pool)):
        from app.services.tenant_db import tenant_connection

        with tenant_scope(42):
            async with tenant_connection() as c:
                assert c is conn

    # fetchval should have been called exactly once with the org_id GUC.
    conn.fetchval.assert_called_once()
    call_args = conn.fetchval.call_args
    sql = call_args[0][0]
    assert "app.org_id" in sql, (
        f"Expected app.org_id in set_config call, got: {sql!r}"
    )
    # Verify the tenant_id value was passed as a positional arg.
    assert call_args[0][1] == "42", (
        f"Expected tenant_id='42' passed to set_config, got: {call_args[0][1]!r}"
    )


@pytest.mark.asyncio
async def test_tenant_connection_does_not_set_legacy_restaurant_guc():
    """Wave 2: tenant_connection() must NOT set app.restaurant_id."""
    conn = _make_mock_conn()
    pool = _make_mock_pool(conn)

    with patch("app.services.database.get_pool", new=AsyncMock(return_value=pool)):
        from app.services.tenant_db import tenant_connection

        with tenant_scope(7):
            async with tenant_connection() as _:
                pass

    # Inspect every call to fetchval and execute for the legacy GUC.
    all_calls = conn.fetchval.call_args_list + conn.execute.call_args_list
    for c in all_calls:
        sql = c[0][0] if c[0] else ""
        assert "app.restaurant_id" not in sql, (
            f"Legacy app.restaurant_id GUC must not be set in Wave 2. "
            f"Found in call: {sql!r}"
        )


@pytest.mark.asyncio
async def test_tenant_connection_bypass_uses_role_not_guc():
    """In bypass mode: SET LOCAL ROLE mesio_superadmin, no set_config call."""
    conn = _make_mock_conn()
    pool = _make_mock_pool(conn)

    with patch("app.services.database.get_pool", new=AsyncMock(return_value=pool)):
        from app.services.tenant_db import tenant_connection
        from app.services.tenant_context import bypass_tenant_scope

        with bypass_tenant_scope("test_bypass_role_check"):
            async with tenant_connection() as _:
                pass

    # fetchval should NOT have been called (no set_config for bypass).
    conn.fetchval.assert_not_called()
    # execute should have been called with the ROLE switch.
    conn.execute.assert_called_once()
    execute_sql = conn.execute.call_args[0][0]
    assert "mesio_superadmin" in execute_sql, (
        f"Bypass mode should SET LOCAL ROLE mesio_superadmin, got: {execute_sql!r}"
    )


@pytest.mark.asyncio
async def test_tenant_connection_no_scope_raises():
    """Without tenant_scope or bypass: TenantNotSetError before touching the pool."""
    from app.services.tenant_db import tenant_connection
    from app.services.tenant_context import TenantNotSetError

    with pytest.raises(TenantNotSetError):
        async with tenant_connection() as _:
            pass


@pytest.mark.asyncio
async def test_tenant_scope_parameter_still_called_org_id():
    """tenant_scope() accepts a positive int (org_id) without error."""
    entered = []
    with tenant_scope(99) as pinned:
        entered.append(pinned)
    assert entered == [99]


@pytest.mark.asyncio
async def test_tenant_scope_zero_raises():
    """tenant_scope(0) must raise ValueError (not a valid org_id)."""
    from app.services.tenant_context import tenant_scope as ts

    with pytest.raises(ValueError, match="must be > 0"):
        with ts(0):
            pass


@pytest.mark.asyncio
async def test_tenant_scope_non_int_raises():
    """tenant_scope('abc') must raise ValueError."""
    from app.services.tenant_context import tenant_scope as ts

    with pytest.raises(ValueError, match="must be a positive int"):
        with ts("abc"):  # type: ignore[arg-type]
            pass
