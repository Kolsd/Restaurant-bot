"""
tests/test_wave2_org_id_join_fix.py

Locks down the Wave-2 org_id JOIN fix across 5 repository functions.

Each function previously filtered `r.id = $N` against the restaurants VIEW,
where `r.id` is actually `location_id`. Post-Wave-2, callers pass `org_id`
(the tenant key), so the filter must use `JOIN locations l ON l.id = r.id
WHERE l.org_id = $N`.

Tests deliberately use org_id=42 while any plausible location_id would be
a different value (e.g. 100), making the distinction testable via SQL string
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


# ── 1. weekly_reports_repo.compute_weekly_stats ───────────────────────────────

@pytest.mark.asyncio
async def test_compute_weekly_stats_uses_org_id():
    """delivery_row query must filter by l.org_id, NOT r.id."""
    from datetime import date, timedelta
    from app.repositories.weekly_reports_repo import compute_weekly_stats

    # compute_weekly_stats executes 3 fetchrow calls (delivery, table, nps) then
    # 1 fetchval call (dormant_count).
    delivery_row = {
        "revenue_current": 0, "revenue_previous": 0,
        "count_current": 0, "count_previous": 0,
    }
    table_row = {
        "revenue_current": 0, "revenue_previous": 0,
        "count_current": 0, "count_previous": 0,
    }
    nps_row = {"nps_count": 0, "nps_avg": None, "reviews_public_new": 0}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[delivery_row, table_row, nps_row])
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)  # dormant_count

    patch_path = "app.repositories.weekly_reports_repo._tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        week_start = date.today()
        week_end = week_start + timedelta(days=7)
        await compute_weekly_stats(42, week_start, week_end)

    sql = _captured_sql(conn)
    assert "l.org_id" in sql, "Must filter by l.org_id (Wave-2 tenant key)"
    assert "join locations" in sql, "Must JOIN locations to resolve org_id"
    # The delivery (orders) query must use l.org_id, not a bare WHERE r.id = $1.
    # (NPS query may legitimately use JOIN restaurants r ON r.id = $1 to get
    # whatsapp_number — that's fine; we assert the org_id pattern is present
    # to confirm the delivery fix landed.)
    assert "l.org_id = $1" in sql, "Delivery query must specifically use l.org_id = $1"


# ── 2. conversations_repo.db_save_nps_waiting (bot_number resolution) ─────────

@pytest.mark.asyncio
async def test_db_save_nps_waiting_resolves_org_id():
    """When restaurant_id is not provided, resolution must return org_id
    (not location_id) by joining locations."""
    from app.repositories.conversations_repo import db_save_nps_waiting

    conn = _make_conn()
    # fetchval for org_id resolution, execute for INSERT
    conn.fetchval = AsyncMock(return_value=42)
    conn.execute = AsyncMock()

    patch_path = "app.repositories.conversations_repo._tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        await db_save_nps_waiting("+573001234567", "+5713000001")  # no restaurant_id

    sql = _captured_sql(conn)
    assert "l.org_id" in sql, "Resolution query must SELECT l.org_id"
    assert "join locations" in sql, "Must JOIN locations to resolve org_id"
    # Verify resolved org_id (42) is inserted — not a location_id
    insert_args = conn.execute.call_args
    assert insert_args is not None
    assert insert_args.args[3] == 42, "INSERT must use the resolved org_id=42"


@pytest.mark.asyncio
async def test_db_save_nps_waiting_passthrough_org_id():
    """When restaurant_id IS provided by caller, it must be used directly
    (caller already holds org_id post-Wave-2)."""
    from app.repositories.conversations_repo import db_save_nps_waiting

    conn = _make_conn()
    conn.execute = AsyncMock()

    patch_path = "app.repositories.conversations_repo._tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        await db_save_nps_waiting("+573001234567", "+5713000001", restaurant_id=42)

    # fetchval must NOT be called when restaurant_id is provided
    conn.fetchval.assert_not_called()
    insert_args = conn.execute.call_args
    assert insert_args is not None
    assert insert_args.args[3] == 42, "INSERT must use caller-supplied org_id=42"


# ── 3. conversations_repo.db_get_customer_order_history ───────────────────────

@pytest.mark.asyncio
async def test_db_get_customer_order_history_uses_org_id():
    """Both COUNT and item-explode queries must filter by l.org_id."""
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
    assert "l.org_id" in sql, "Must filter by l.org_id in order history queries"
    assert "join locations" in sql, "Must JOIN locations to resolve org_id"
    # Verify bare r.id = $2 is gone from both queries
    assert "r.id           = $2" not in sql, "Must NOT use bare r.id = $2"
    assert "r.id = $2" not in sql, "Must NOT use bare r.id = $2"


# ── 4. orders_repo.db_get_delivery_orders ─────────────────────────────────────

@pytest.mark.asyncio
async def test_db_get_delivery_orders_uses_org_id():
    """With restaurant_id provided, query must filter by org_id (Wave-2 tenant key)."""
    from app.repositories.orders_repo import db_get_delivery_orders

    conn = _make_conn(fetch_val=[])

    # orders_repo uses _tenant_connection() (local helper wrapping tenant_connection)
    patch_path = "app.repositories.orders_repo._tenant_connection"
    with patch(patch_path, _make_tenant_conn_ctx(conn)):
        result = await db_get_delivery_orders(["pendiente"], restaurant_id=42)

    assert result == []
    sql = _captured_sql(conn)
    # The query filters by orders.org_id directly — no JOIN to locations is
    # needed since orders has its own org_id column post-Wave-2. The previous
    # JOIN multiplied rows by locations count (same whatsapp_number).
    assert "org_id = $2" in sql, "Must filter by org_id (Wave-2 tenant key)"
    assert "r.id = $2" not in sql, "Must NOT use bare r.id = $2 (pre-Wave-2 pattern)"


# ── 5. tables_repo.db_get_delivery_status_hash_for_restaurant ─────────────────

@pytest.mark.asyncio
async def test_db_get_delivery_status_hash_uses_org_id():
    """Must JOIN locations and filter by l.org_id, not r.id."""
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
    assert "l.org_id" in sql, "Must filter by l.org_id (Wave-2 tenant key)"
    assert "join locations" in sql, "Must JOIN locations"
    assert "r.id = $1" not in sql, "Must NOT use bare r.id = $1"
