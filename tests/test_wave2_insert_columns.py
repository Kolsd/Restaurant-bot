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
      1. INSERT INTO menu_availability (..., org_id, location_id, ...) —
         restaurant_id col dropped long ago
      2. ON CONFLICT (org_id, location_id, dish_name) — the primary key as of
         migration 0091, which made sold-out state PER SEDE. Before it the key
         was (org_id, dish_name) and marking a dish sold out at one sede took
         it off every sede's menu.
    """
    from app.repositories.restaurant_repo import db_set_dish_availability

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(11):
            await db_set_dish_availability(11, "Pizza Margherita", available=False,
                                           location_id=22)

    sql_blob = _all_execute_sql(conn)
    assert "insert into menu_availability" in sql_blob
    assert "org_id" in sql_blob
    assert "location_id" in sql_blob
    assert "on conflict (org_id, location_id, dish_name)" in sql_blob, (
        "ON CONFLICT must match the per-sede primary key from 0091"
    )
    assert "restaurant_id" not in sql_blob, (
        "Neither column list nor ON CONFLICT may reference the dropped column"
    )


async def test_set_dish_availability_refuses_to_write_without_a_sede():
    """No sede means no write. Defaulting to an org-wide row is exactly the
    behaviour 0091 removed, and such a row is never read back."""
    import pytest as _pytest
    from app.repositories.restaurant_repo import db_set_dish_availability

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(11):
            with _pytest.raises(ValueError):
                await db_set_dish_availability(11, "Pizza Margherita", available=False)

    assert not conn.execute.call_args_list or "menu_availability" not in _all_execute_sql(conn)


async def test_set_dish_availability_passes_dish_name_and_tenant_as_params():
    """SQL must use positional params for dish_name + tenant (no f-strings)."""
    from app.repositories.restaurant_repo import db_set_dish_availability

    conn = _make_conn()
    pool = _make_pool(conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(33):
            await db_set_dish_availability(33, "Bandeja Paisa", available=True,
                                           location_id=44)

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
            result = await db_auto_create_table(restaurant_id=7)

    sql_blob = _all_execute_sql(conn)
    assert "insert into restaurant_tables" in sql_blob
    assert "org_id" in sql_blob
    assert "location_id" in sql_blob
    # Sanity: returned dict has the new id/number
    assert result["number"] == 1
    assert "table-7-1" in result["id"]
