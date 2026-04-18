"""
tests/test_wave2_insert_columns.py

Locks down the Wave-2 schema migration for INSERT paths that previously
referenced the dropped restaurant_id column OR omitted the now-required
org_id / location_id columns.

Covered paths:
  app/repositories/restaurant_repo.py:
    - _sync_staff_shift          (offline sync handler for staff_shifts)
    - _sync_staff                (offline sync handler for staff)
    - db_set_dish_availability   (dashboard "mark dish out-of-stock")

  app/repositories/tables_repo.py:
    - db_create_table            (admin creates a table from dashboard)
    - db_auto_create_table       (admin auto-creates next-available table)

Each test asserts the SQL passed to conn.execute uses the new column
names (org_id, location_id) and NOT the dropped legacy column
(restaurant_id). Restaurant_tables specifically has BOTH org_id NOT NULL
AND location_id NOT NULL (location_id is NOT in 0037d's _RELAX_TABLES),
so any INSERT that omits either would crash with NotNullViolation the
first time an admin creates a table post-Wave-2.

If a future refactor accidentally reintroduces 'restaurant_id' or
removes 'org_id'/'location_id' from any of these INSERTs, these tests
fail loudly — much better than the runtime error in production.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant_context import tenant_scope


def _make_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)  # for set_config calls
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool(conn) -> MagicMock:
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _all_execute_sql(conn) -> str:
    """Concatenate every SQL string passed to conn.execute, lower-cased."""
    return " ".join(
        str(c.args[0]) for c in conn.execute.call_args_list if c.args
    ).lower()


# ── _sync_staff_shift ─────────────────────────────────────────────────────────

async def test_sync_staff_shift_inserts_org_id_not_restaurant_id():
    """
    _sync_staff_shift must INSERT into staff_shifts using the org_id column.
    The restaurant_id column was dropped from staff_shifts in migration 0037
    — referencing it now would crash with UndefinedColumnError at runtime.
    """
    from app.repositories.restaurant_repo import _sync_staff_shift

    conn = _make_conn()
    await _sync_staff_shift(
        conn,
        restaurant_id=42,
        data={
            "id": "shift-uuid-1",
            "staff_id": "staff-uuid-1",
            "clock_in": "2026-04-18T09:00:00Z",
            "clock_out": "2026-04-18T17:00:00Z",
            "notes": "test",
        },
    )

    sql_blob = _all_execute_sql(conn)
    assert "insert into staff_shifts" in sql_blob
    assert "org_id" in sql_blob, "INSERT must reference org_id (Wave-2 canonical)"
    # Must NOT mention the dropped legacy column anywhere in the staff_shifts insert
    assert "restaurant_id" not in sql_blob, (
        "INSERT must NOT reference the dropped restaurant_id column"
    )


async def test_sync_staff_shift_passes_tenant_id_as_param():
    """The tenant integer must be passed as a positional param (no f-strings)."""
    from app.repositories.restaurant_repo import _sync_staff_shift

    conn = _make_conn()
    await _sync_staff_shift(
        conn,
        restaurant_id=99,
        data={"id": "x", "staff_id": "y", "clock_in": "2026-04-18T09:00:00Z"},
    )
    # The third positional arg to execute (after the SQL) is restaurant_id
    args = conn.execute.call_args.args
    # args[0] is the SQL; subsequent are params
    assert 99 in args[1:], "Tenant id must be passed as a query parameter"


# ── _sync_staff ───────────────────────────────────────────────────────────────

async def test_sync_staff_inserts_org_id_not_restaurant_id():
    """_sync_staff must INSERT into staff using org_id (restaurant_id dropped)."""
    from app.repositories.restaurant_repo import _sync_staff

    conn = _make_conn()
    await _sync_staff(
        conn,
        restaurant_id=7,
        data={
            "id": "staff-uuid-1",
            "name": "Carlos",
            "role": "mesero",
            "pin": "$2b$hash",
            "active": True,
        },
    )

    sql_blob = _all_execute_sql(conn)
    assert "insert into staff" in sql_blob
    assert "org_id" in sql_blob
    assert "restaurant_id" not in sql_blob


# ── db_set_dish_availability ──────────────────────────────────────────────────

async def test_set_dish_availability_uses_org_id_in_insert_and_on_conflict():
    """
    db_set_dish_availability must:
      1. INSERT INTO menu_availability (..., org_id, ...) — restaurant_id col dropped
      2. ON CONFLICT (dish_name, org_id) — the unique constraint was recreated
         with org_id in 0037b (the legacy (dish_name, restaurant_id) constraint
         was dropped CASCADE with the column).
    """
    from app.repositories.restaurant_repo import db_set_dish_availability

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(11):
            await db_set_dish_availability(11, "Pizza Margherita", available=False)

    sql_blob = _all_execute_sql(conn)
    assert "insert into menu_availability" in sql_blob
    assert "org_id" in sql_blob
    assert "on conflict (dish_name, org_id)" in sql_blob, (
        "ON CONFLICT must match the post-Wave-2 unique constraint shape"
    )
    assert "restaurant_id" not in sql_blob, (
        "Neither column list nor ON CONFLICT may reference the dropped column"
    )


async def test_set_dish_availability_passes_dish_name_and_tenant_as_params():
    """SQL must use positional params for dish_name + tenant (no f-strings)."""
    from app.repositories.restaurant_repo import db_set_dish_availability

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(33):
            await db_set_dish_availability(33, "Bandeja Paisa", available=True)

    insert_call = next(
        c for c in conn.execute.call_args_list
        if c.args and "menu_availability" in str(c.args[0]).lower()
    )
    args = insert_call.args
    assert "Bandeja Paisa" in args[1:], "dish_name must be a positional param"
    assert 33 in args[1:], "tenant id must be a positional param"
    assert True in args[1:], "availability flag must be a positional param"


# ── db_create_table (admin creates a single table) ───────────────────────────

async def test_create_table_inserts_org_id_and_location_id():
    """
    db_create_table MUST insert into restaurant_tables with org_id (from the
    app.org_id GUC) and location_id (mirror of branch_id) — both columns are
    NOT NULL post-Wave-2 (location_id is NOT in 0037d's _RELAX_TABLES).
    """
    from app.repositories.tables_repo import db_create_table

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(5):
            await db_create_table(
                table_id="t-1", number=1, name="Mesa 1",
                branch_id=5, capacity=4, table_type="interior", zone="",
            )

    sql_blob = _all_execute_sql(conn)
    assert "insert into restaurant_tables" in sql_blob
    assert "org_id" in sql_blob, "INSERT must populate org_id from GUC"
    assert "location_id" in sql_blob, "INSERT must populate location_id (NOT NULL on this table)"
    assert "current_setting('app.org_id'" in sql_blob, (
        "org_id should come from GUC, not from a parameter the caller might forget"
    )


async def test_create_table_on_conflict_preserves_location_id():
    """
    The ON CONFLICT (id) DO UPDATE clause must keep location_id in sync if
    the row is reactivated under a different sede. Otherwise an UPDATE could
    leave a stale location_id while branch_id changes.
    """
    from app.repositories.tables_repo import db_create_table

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(5):
            await db_create_table(table_id="t-1", number=1, name="X", branch_id=5)

    sql_blob = _all_execute_sql(conn)
    # ON CONFLICT update must touch location_id alongside branch_id
    assert "on conflict" in sql_blob
    assert "location_id=excluded.location_id" in sql_blob.replace(" ", ""), (
        "ON CONFLICT update must keep location_id in sync with branch_id"
    )


# ── db_auto_create_table (admin auto-numbered table) ─────────────────────────

async def test_auto_create_table_inserts_org_id_and_location_id():
    """
    db_auto_create_table also writes restaurant_tables and must populate
    org_id + location_id. It builds the table_id internally so the only
    visible side-effect is the SQL passed to conn.execute.
    """
    from app.repositories.tables_repo import db_auto_create_table

    conn = _make_conn()
    # First fetch returns 'no existing tables' so new_number = 1
    conn.fetch = AsyncMock(return_value=[])
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(7):
            result = await db_auto_create_table(restaurant_id=7, is_main_restaurant=False)

    sql_blob = _all_execute_sql(conn)
    assert "insert into restaurant_tables" in sql_blob
    assert "org_id" in sql_blob
    assert "location_id" in sql_blob
    # Sanity: returned dict has the new id/number
    assert result["number"] == 1
    assert "table-7-1" in result["id"]
