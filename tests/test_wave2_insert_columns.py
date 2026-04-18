"""
tests/test_wave2_insert_columns.py

Locks down the Wave-2 schema migration for three INSERT paths in
app/repositories/restaurant_repo.py that previously used the
restaurant_id column (DROPPED in migration 0037):

  - _sync_staff_shift  (offline sync handler for staff_shifts)
  - _sync_staff        (offline sync handler for staff)
  - db_set_dish_availability (dashboard "mark dish out-of-stock")

Each test asserts the SQL passed to conn.execute uses the new column
name (org_id) and NOT the dropped legacy one (restaurant_id). If a
future refactor accidentally reintroduces 'restaurant_id' in any of
these INSERTs, these tests fail loudly — much better than the
runtime UndefinedColumn error that would otherwise hit production
the next time an admin marks a dish unavailable.
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
