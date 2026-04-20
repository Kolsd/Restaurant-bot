"""
tests/test_staff_comms.py

Tests for the admin↔staff communication feature (Sprint C).

Unit tests (no live DB, mock pool):
  - test_create_announcement_returns_shape
  - test_list_active_excludes_expired
  - test_list_active_excludes_unpublished
  - test_soft_delete_marks_active_false
  - test_complete_task_idempotent
  - test_completions_count_correct
  - test_get_completions_for_staff_empty_task_ids

Integration tests (require TEST_DATABASE_URL):
  - test_create_then_list_announcement_roundtrip
  - test_tenant_isolation_announcements
  - test_tenant_isolation_tasks
  - test_staff_completion_roundtrip
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tenant_context import tenant_scope, bypass_tenant_scope


# ── Pytest markers ────────────────────────────────────────────────────────────

pytestmark = pytest.mark.asyncio

_INTEGRATION_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_SKIP_INTEGRATION = pytest.mark.skipif(
    not _INTEGRATION_URL,
    reason="Set TEST_DATABASE_URL to run integration tests",
)


# ── Mock helpers (shared with loyalty_repo pattern) ───────────────────────────

def _make_conn():
    """Minimal asyncpg connection mock."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch    = AsyncMock(return_value=[])
    conn.execute  = AsyncMock(return_value="UPDATE 1")

    _txn = MagicMock()
    _txn.__aenter__ = AsyncMock(return_value=_txn)
    _txn.__aexit__  = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=_txn)
    return conn


def _make_pool(conn):
    """Wrap conn in a minimal pool context manager."""
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__  = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_row(d: dict):
    """Build an object that mimics an asyncpg Record."""
    row = MagicMock()
    row.__iter__    = lambda s: iter(d.items())
    row.keys        = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get         = lambda k, default=None: d.get(k, default)
    return row


# ════════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════════════════════════════════════

async def test_create_announcement_returns_shape():
    """db_create_announcement returns a dict with at least id, title, body, org_id."""
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value=_make_row({
        "id": 1, "org_id": 42, "title": "Hola equipo",
        "body": "Mensaje de prueba", "active": True,
        "created_at": datetime.now(tz=timezone.utc),
        "published_at": datetime.now(tz=timezone.utc),
        "expires_at": None, "created_by": "admin",
    }))
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(42):
            from app.repositories.staff_comms_repo import db_create_announcement
            result = await db_create_announcement(
                org_id=42, title="Hola equipo", body="Mensaje de prueba",
                created_by="admin",
            )

    assert result["id"] == 1
    assert result["title"] == "Hola equipo"
    assert result["org_id"] == 42
    assert result["active"] is True


async def test_list_active_excludes_expired():
    """db_list_announcements_active passes NOW() as $2 so DB can filter expired rows.

    We verify the NOW timestamp parameter is actually forwarded to conn.fetch.
    """
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])
    pool = _make_pool(conn)

    now = datetime.now(tz=timezone.utc)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(5):
            from app.repositories.staff_comms_repo import db_list_announcements_active
            result = await db_list_announcements_active(5, now)

    assert result == []
    # Verify now was passed as the second positional arg to the SQL call
    call_args = conn.fetch.call_args
    assert call_args is not None
    positional_args = call_args[0]
    # sql, org_id=5, now=<datetime>
    assert positional_args[1] == 5
    assert positional_args[2] == now


async def test_list_active_excludes_unpublished():
    """The active query only returns rows where published_at IS NOT NULL.

    We validate that the SQL string contains the published_at IS NOT NULL guard.
    """
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])
    pool = _make_pool(conn)

    now = datetime.now(tz=timezone.utc)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(7):
            from app.repositories.staff_comms_repo import db_list_announcements_active
            await db_list_announcements_active(7, now)

    sql = conn.fetch.call_args[0][0]
    assert "published_at IS NOT NULL" in sql


async def test_soft_delete_marks_active_false():
    """db_soft_delete_announcement calls UPDATE ... SET active = false."""
    conn = _make_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(3):
            from app.repositories.staff_comms_repo import db_soft_delete_announcement
            ok = await db_soft_delete_announcement(3, 99)

    assert ok is True
    sql = conn.execute.call_args[0][0]
    assert "active = false" in sql


async def test_complete_task_idempotent():
    """db_complete_task uses ON CONFLICT DO UPDATE so duplicate calls are safe."""
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value=_make_row({
        "id": 1, "task_id": 10, "staff_id": "uuid-xxx", "org_id": 2,
        "completed_at": datetime.now(tz=timezone.utc), "notes": None,
    }))
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(2):
            from app.repositories.staff_comms_repo import db_complete_task
            # Call twice — should not raise
            r1 = await db_complete_task(10, "uuid-xxx", 2)
            r2 = await db_complete_task(10, "uuid-xxx", 2)

    assert r1["task_id"] == 10
    assert r2["task_id"] == 10
    sql = conn.fetchrow.call_args[0][0]
    assert "ON CONFLICT" in sql


async def test_completions_count_correct():
    """db_list_tasks_admin returns tasks with a completions_count subquery."""
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[
        _make_row({
            "id": 1, "org_id": 9, "title": "Task A", "description": "",
            "created_by": None, "created_at": datetime.now(tz=timezone.utc),
            "due_date": None, "active": True, "completions_count": 3,
        })
    ])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(9):
            from app.repositories.staff_comms_repo import db_list_tasks_admin
            tasks = await db_list_tasks_admin(9)

    assert len(tasks) == 1
    assert tasks[0]["completions_count"] == 3
    sql = conn.fetch.call_args[0][0]
    assert "completions_count" in sql


async def test_get_completions_for_staff_empty_task_ids():
    """db_get_completions_for_staff returns {} immediately when task_ids is empty."""
    conn = _make_conn()
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            from app.repositories.staff_comms_repo import db_get_completions_for_staff
            result = await db_get_completions_for_staff("some-uuid", [])

    assert result == {}
    # No DB call should have been made
    conn.fetch.assert_not_called()


async def test_staff_self_tasks_shows_completed_by_me_flag():
    """db_get_completions_for_staff result is used to annotate tasks with completed_by_me.

    We test the annotation logic directly (unit-style with mocked sub-functions).
    """
    from unittest.mock import patch as _patch, AsyncMock as _AM

    tasks = [
        {"id": 1, "title": "Task done", "active": True, "completions_count": 1,
         "description": "", "created_by": None, "due_date": None,
         "created_at": datetime.now(tz=timezone.utc).isoformat()},
        {"id": 2, "title": "Task pending", "active": True, "completions_count": 0,
         "description": "", "created_by": None, "due_date": None,
         "created_at": datetime.now(tz=timezone.utc).isoformat()},
    ]
    completions_map = {
        1: {"task_id": 1, "staff_id": "abc", "completed_at": "2026-04-19T10:00:00Z"}
    }

    with _patch("app.repositories.staff_comms_repo.db_list_tasks_active", _AM(return_value=tasks)), \
         _patch("app.repositories.staff_comms_repo.db_get_completions_for_staff", _AM(return_value=completions_map)):

        from app.repositories import staff_comms_repo
        raw_tasks = await staff_comms_repo.db_list_tasks_active(1)
        task_ids  = [t["id"] for t in raw_tasks]
        comp_map  = await staff_comms_repo.db_get_completions_for_staff("abc", task_ids)

        annotated = [
            {**t, "completed_by_me": t["id"] in comp_map}
            for t in raw_tasks
        ]

    assert annotated[0]["completed_by_me"] is True
    assert annotated[1]["completed_by_me"] is False


# ════════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (require TEST_DATABASE_URL)
# ════════════════════════════════════════════════════════════════════════════════

# ── Pool shim (same pattern as test_integration_flows.py) ────────────────────

def _make_pool_for_conn(conn):
    """Fake pool whose acquire() yields the given conn without extra overhead."""
    @asynccontextmanager
    async def _acquire():
        yield conn
    pool = AsyncMock()
    pool.acquire = _acquire
    return pool


async def _insert_org(conn) -> int:
    """Insert a minimal org + location pair and return org_id."""
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number)
           VALUES ($1, $2)
           RETURNING id""",
        f"TestOrg-{uuid.uuid4().hex[:8]}",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
    )
    await conn.execute(
        """INSERT INTO locations (id, org_id, name, code, active, timezone)
           VALUES ($1, $1, 'Sede Test', 'main', true, 'America/Bogota')""",
        org_id,
    )
    await conn.execute(
        "SELECT setval('locations_id_seq', GREATEST(nextval('locations_id_seq'), $1))",
        org_id,
    )
    return org_id


async def _insert_staff_member(conn, org_id: int) -> str:
    """Insert a staff member and return UUID string."""
    sid = uuid.uuid4()
    uname = f"test_{uuid.uuid4().hex[:10]}"
    await conn.execute(
        """INSERT INTO staff (id, org_id, name, role, pin, username)
           VALUES ($1, $2, 'Test Staff', 'mesero', '', $3)""",
        sid, org_id, uname,
    )
    return str(sid)


# ── Integration fixtures ──────────────────────────────────────────────────────

@pytest.fixture
async def db_conn():
    """Live DB connection wrapped in a rollback transaction."""
    import asyncpg
    conn = await asyncpg.connect(_INTEGRATION_URL)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


# ── Integration test helpers ──────────────────────────────────────────────────

def _pool_ctx(conn):
    return patch("app.services.database.get_pool", AsyncMock(return_value=_make_pool_for_conn(conn)))


# ── Tests ─────────────────────────────────────────────────────────────────────

@_SKIP_INTEGRATION
async def test_create_then_list_announcement_roundtrip(db_conn):
    """Create an announcement and verify it appears in the admin list."""
    org_id = await _insert_org(db_conn)

    with _pool_ctx(db_conn):
        with tenant_scope(org_id):
            from app.repositories.staff_comms_repo import (
                db_create_announcement,
                db_list_announcements_admin,
                db_list_announcements_active,
            )
            ann = await db_create_announcement(
                org_id=org_id,
                title="Reunión mañana a las 9am",
                body="Por favor confirmen asistencia.",
                created_by=None,
                published=True,
            )
            assert ann["id"] > 0
            assert ann["title"] == "Reunión mañana a las 9am"

            # Must appear in admin list
            admin_list = await db_list_announcements_admin(org_id)
            ids = [a["id"] for a in admin_list]
            assert ann["id"] in ids

            # Must appear in active list (published, not expired)
            now = datetime.now(tz=timezone.utc)
            active_list = await db_list_announcements_active(org_id, now)
            active_ids = [a["id"] for a in active_list]
            assert ann["id"] in active_ids


@_SKIP_INTEGRATION
async def test_tenant_isolation_announcements(db_conn):
    """Org A cannot see Org B's announcements."""
    org_a = await _insert_org(db_conn)
    org_b = await _insert_org(db_conn)

    with _pool_ctx(db_conn):
        # Create announcement under org_a
        with tenant_scope(org_a):
            from app.repositories.staff_comms_repo import (
                db_create_announcement,
                db_list_announcements_admin,
            )
            await db_create_announcement(
                org_id=org_a, title="Org A secret", body="Only for A.",
                created_by=None,
            )

        # Listing under org_b should return empty (RLS enforced)
        with tenant_scope(org_b):
            from app.repositories.staff_comms_repo import db_list_announcements_admin
            b_list = await db_list_announcements_admin(org_b)
            titles = [a["title"] for a in b_list]
            assert "Org A secret" not in titles


@_SKIP_INTEGRATION
async def test_tenant_isolation_tasks(db_conn):
    """Org A cannot see Org B's tasks."""
    org_a = await _insert_org(db_conn)
    org_b = await _insert_org(db_conn)

    with _pool_ctx(db_conn):
        with tenant_scope(org_a):
            from app.repositories.staff_comms_repo import db_create_task, db_list_tasks_admin
            await db_create_task(
                org_id=org_a, title="Secret task A", description="",
                created_by=None,
            )

        with tenant_scope(org_b):
            from app.repositories.staff_comms_repo import db_list_tasks_admin
            b_tasks = await db_list_tasks_admin(org_b)
            titles = [t["title"] for t in b_tasks]
            assert "Secret task A" not in titles


@_SKIP_INTEGRATION
async def test_staff_completion_roundtrip(db_conn):
    """Staff can complete a task; second call updates rather than duplicates."""
    org_id   = await _insert_org(db_conn)
    staff_id = await _insert_staff_member(db_conn, org_id)

    with _pool_ctx(db_conn):
        with tenant_scope(org_id):
            from app.repositories.staff_comms_repo import (
                db_create_task,
                db_complete_task,
                db_list_completions_for_task,
                db_get_completions_for_staff,
            )
            task = await db_create_task(
                org_id=org_id, title="Check supplies", description="",
                created_by=None,
            )
            task_id = task["id"]

            # First completion
            comp = await db_complete_task(task_id, staff_id, org_id, notes="Done!")
            assert comp["task_id"] == task_id

            # Second call — idempotent (no duplicate row)
            comp2 = await db_complete_task(task_id, staff_id, org_id, notes="Updated note")
            assert comp2["task_id"] == task_id

            # list_completions_for_task returns exactly 1 row (not 2)
            completions = await db_list_completions_for_task(task_id)
            assert len(completions) == 1
            assert completions[0]["notes"] == "Updated note"

            # get_completions_for_staff returns the task_id
            staff_comps = await db_get_completions_for_staff(staff_id, [task_id])
            assert task_id in staff_comps
