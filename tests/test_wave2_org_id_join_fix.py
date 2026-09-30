"""
tests/test_wave2_org_id_join_fix.py

Locks down that these repository functions filter by the row's own
`org_id` — never `r.id` of the restaurants VIEW (a location id) and never
the retired WhatsApp key (`bot_number` / `whatsapp_number`, 0098).

Tests deliberately use org_id=42 while any plausible location_id would be
a different value, making the distinction testable via SQL string
assertions.
"""
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_conn(fetchrow_val=None, fetch_val=None, fetchval_val=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_val)
    conn.fetch = AsyncMock(return_value=fetch_val if fetch_val is not None else [])
    conn.fetchval = AsyncMock(return_value=fetchval_val if fetchval_val is not None else 0)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_tenant_conn_ctx(conn):
    """Returns an async context manager that yields `conn`, suitable for
    patching `tenant_connection` or `_tenant_connection`."""
    @asynccontextmanager
    async def _ctx():
        yield conn
    return _ctx


def _captured_sql(conn) -> str:
    """Collect all SQL strings passed to any conn method call."""
    parts = []
    for method in (conn.fetchrow, conn.fetch, conn.fetchval, conn.execute):
        for call in method.call_args_list:
            if call.args:
                parts.append(str(call.args[0]))
    return " ".join(parts).lower()


# ── 2. conversations_repo.db_save_nps_waiting ────────────────────────────────


@pytest.mark.asyncio
async def test_db_save_nps_waiting_writes_org_id():
    """The pending-NPS row is keyed by (phone, org_id)."""
    from app.repositories.conversations_repo import db_save_nps_waiting

    conn = _make_conn()
    conn.execute = AsyncMock()

    patch_path = "app.repositories.conversations_repo._tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        await db_save_nps_waiting("+573001234567", 42)

    sql = _captured_sql(conn)
    assert "on conflict (phone, org_id)" in sql
    assert "bot_number" not in sql
    assert conn.execute.call_args.args[1:] == ("+573001234567", 42)


# ── 3. conversations_repo.db_get_customer_order_history ───────────────────────

@pytest.mark.asyncio
async def test_db_get_customer_order_history_uses_org_id():
    """Both COUNT and item-explode queries must filter by orders.org_id."""
    from app.repositories.conversations_repo import db_get_customer_order_history

    conn = _make_conn()
    # fetchval for COUNT (return >=2 so Step 2 runs)
    conn.fetchval = AsyncMock(return_value=5)
    conn.fetch = AsyncMock(return_value=[])

    patch_path = "app.repositories.conversations_repo._tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        result = await db_get_customer_order_history("+573001234567", 42)

    assert result == []
    sql = _captured_sql(conn)
    assert sql.count("o.org_id       = $2") == 2, "Both queries must filter o.org_id"
    assert "whatsapp_number" not in sql and "bot_number" not in sql
    assert "r.id = $2" not in sql, "Must NOT use bare r.id = $2"


# ── 5. tables_repo.db_get_delivery_status_hash_for_restaurant ─────────────────

@pytest.mark.asyncio
async def test_db_get_delivery_status_hash_uses_org_id():
    """Must filter by orders.org_id, not r.id nor the WhatsApp key."""
    from app.repositories.tables_repo import db_get_delivery_status_hash_for_restaurant

    conn = _make_conn(fetch_val=[])

    # tables_repo imports tenant_connection from tenant_db directly
    patch_path = "app.repositories.tables_repo.tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        from app.services.tenant_context import tenant_scope
        with tenant_scope(42):
            result = await db_get_delivery_status_hash_for_restaurant(42)

    assert result == []
    sql = _captured_sql(conn)
    assert "o.org_id = $1" in sql
    assert "whatsapp_number" not in sql and "bot_number" not in sql
    assert "r.id = $1" not in sql, "Must NOT use bare r.id = $1"
