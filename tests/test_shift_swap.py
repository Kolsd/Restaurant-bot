"""
tests/test_shift_swap.py

Tests for Sprint W — Shift Swap feature.

Unit tests (mocked pool, no live DB):
  - test_create_swap_returns_pending
  - test_target_accept_transitions_state
  - test_target_reject_transitions_state
  - test_admin_approve_calls_execute_swap
  - test_invalid_transition_raises

Integration tests (require TEST_DATABASE_URL):
  - test_full_happy_path_roundtrip
  - test_tenant_isolation_swap
  - test_target_cannot_approve_admin_only
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tenant_context import tenant_scope

pytestmark = pytest.mark.asyncio

_INTEGRATION_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_SKIP_INTEGRATION = pytest.mark.skipif(
    not _INTEGRATION_URL,
    reason="Set TEST_DATABASE_URL to run integration tests",
)


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_conn():
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
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__  = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_row(d: dict):
    row = MagicMock()
    row.__iter__    = lambda s: iter(d.items())
    row.keys        = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get         = lambda k, default=None: d.get(k, default)
    return row


def _fake_swap_row(overrides: dict | None = None) -> dict:
    req_id = str(uuid.uuid4())
    tgt_id = str(uuid.uuid4())
    base = {
        "id": 1,
        "org_id": 42,
        "requester_staff_id": req_id,
        "target_staff_id": tgt_id,
        "shift_date": date(2026, 5, 10),
        "shift_start_time": None,
        "shift_end_time": None,
        "reason": None,
        "status": "pending",
        "decided_by": None,
        "decided_at": None,
        "admin_notes": None,
        "created_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
        "requester_name": "Alice",
        "target_name": "Bob",
    }
    if overrides:
        base.update(overrides)
    return base


# ════════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════════════════════════════════════

async def test_create_swap_returns_pending():
    """db_create_shift_swap inserts a row with status='pending'."""
    conn = _make_conn()
    inserted = _fake_swap_row({"status": "pending"})
    conn.fetchrow = AsyncMock(return_value=_make_row(inserted))
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_comms_repo import db_create_shift_swap
        with tenant_scope(42):
            result = await db_create_shift_swap(
                org_id=42,
                requester_staff_id=inserted["requester_staff_id"],
                target_staff_id=inserted["target_staff_id"],
                shift_date=date(2026, 5, 10),
            )

    assert result["status"] == "pending"
    # Verify the INSERT was called with the right positional values
    call_args = conn.fetchrow.call_args
    assert "INSERT INTO shift_swap_requests" in call_args[0][0]


async def test_target_accept_transitions_state():
    """db_update_swap_status pending → target_accepted succeeds."""
    conn = _make_conn()
    pending_row    = _fake_swap_row({"id": 7, "status": "pending"})
    accepted_row   = _fake_swap_row({"id": 7, "status": "target_accepted"})

    # First fetchrow (db_get_swap inside db_update_swap_status) → pending row
    # Second fetchrow (UPDATE ... RETURNING) → accepted row
    conn.fetchrow = AsyncMock(side_effect=[
        _make_row(pending_row),
        _make_row(accepted_row),
    ])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_comms_repo import db_update_swap_status
        with tenant_scope(42):
            result = await db_update_swap_status(
                org_id=42,
                swap_id=7,
                new_status="target_accepted",
                decided_by=pending_row["target_staff_id"],
            )

    assert result["status"] == "target_accepted"


async def test_target_reject_transitions_state():
    """db_update_swap_status pending → target_rejected succeeds."""
    conn = _make_conn()
    pending_row  = _fake_swap_row({"id": 3, "status": "pending"})
    rejected_row = _fake_swap_row({"id": 3, "status": "target_rejected"})

    conn.fetchrow = AsyncMock(side_effect=[
        _make_row(pending_row),
        _make_row(rejected_row),
    ])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_comms_repo import db_update_swap_status
        with tenant_scope(42):
            result = await db_update_swap_status(
                org_id=42,
                swap_id=3,
                new_status="target_rejected",
            )

    assert result["status"] == "target_rejected"


async def test_admin_approve_calls_execute_swap():
    """db_update_swap_status target_accepted → admin_approved, then db_execute_swap swaps shifts."""
    conn = _make_conn()
    swap_data = _fake_swap_row({
        "id": 5,
        "status": "target_accepted",
        "shift_date": date(2026, 5, 10),
    })
    approved_row = _fake_swap_row({"id": 5, "status": "admin_approved"})

    req_shift_row = _make_row({"id": 101, "staff_id": swap_data["requester_staff_id"]})
    tgt_shift_row = _make_row({"id": 202, "staff_id": swap_data["target_staff_id"]})

    # Call sequence:
    # 1. db_get_swap (inside update_swap_status) → swap_data
    # 2. UPDATE RETURNING → approved_row
    # 3. db_get_swap (inside execute_swap) → approved_row
    # 4. SELECT req shift → req_shift_row
    # 5. SELECT tgt shift → tgt_shift_row
    conn.fetchrow = AsyncMock(side_effect=[
        _make_row(swap_data),       # 1 — get_swap for update_swap_status
        _make_row(approved_row),    # 2 — UPDATE RETURNING
        _make_row(approved_row),    # 3 — get_swap for execute_swap
        req_shift_row,              # 4 — requester shift lookup
        tgt_shift_row,              # 5 — target shift lookup
    ])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_comms_repo import db_update_swap_status, db_execute_swap
        with tenant_scope(42):
            updated = await db_update_swap_status(
                org_id=42, swap_id=5, new_status="admin_approved", decided_by="admin_user"
            )
            exec_result = await db_execute_swap(org_id=42, swap_id=5)

    assert updated["status"] == "admin_approved"
    assert exec_result["swapped"] is True
    assert exec_result["requester_shift_id"] == 101
    assert exec_result["target_shift_id"] == 202
    # The two UPDATE statements were called
    assert conn.execute.call_count >= 2


async def test_invalid_transition_raises():
    """db_update_swap_status raises ValueError for disallowed transitions."""
    conn = _make_conn()
    pending_row = _fake_swap_row({"id": 9, "status": "pending"})
    conn.fetchrow = AsyncMock(return_value=_make_row(pending_row))
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_comms_repo import db_update_swap_status
        with tenant_scope(42):
            with pytest.raises(ValueError, match="Invalid transition"):
                await db_update_swap_status(
                    org_id=42, swap_id=9, new_status="admin_approved"
                )


# ════════════════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ════════════════════════════════════════════════════════════════════════════════

class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


class _ConnProxy:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture
async def db_conn():
    """Isolated asyncpg connection rolled back after each test."""
    import asyncpg
    conn = await asyncpg.connect(_INTEGRATION_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _insert_org(conn) -> int:
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number)
           VALUES ($1, $2) RETURNING id""",
        f"SwapTestOrg-{uuid.uuid4().hex[:8]}",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
    )
    await conn.execute(
        """INSERT INTO locations (org_id, name, code, active, timezone)
           VALUES ($1, 'Sede Test', 'main', true, 'America/Bogota')""",
        org_id,
    )
    return org_id


async def _insert_staff(conn, org_id: int, name: str = "Test Staff") -> str:
    sid   = uuid.uuid4()
    uname = f"test_{uuid.uuid4().hex[:10]}"
    await conn.execute(
        """INSERT INTO staff (id, org_id, name, role, pin, username)
           VALUES ($1, $2, $3, 'mesero', '', $4)""",
        sid, org_id, name, uname,
    )
    return str(sid)


# ════════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ════════════════════════════════════════════════════════════════════════════════

@_SKIP_INTEGRATION
async def test_full_happy_path_roundtrip(db_conn):
    """Full flow: create → target accepts → admin approves.

    Because no staff_shifts exist for the test date, the physical swap is
    skipped (swapped=False) but all status transitions complete successfully.
    """
    org_id   = await _insert_org(db_conn)
    req_id   = await _insert_staff(db_conn, org_id, "Requester")
    tgt_id   = await _insert_staff(db_conn, org_id, "Target")

    proxy = _ConnProxy(db_conn)
    shim  = _PoolShim(proxy)

    with patch("app.services.database.get_pool", AsyncMock(return_value=shim)):
        from app.repositories.staff_comms_repo import (
            db_create_shift_swap, db_update_swap_status, db_execute_swap, db_get_swap,
        )
        with tenant_scope(org_id):
            # Step 1: requester creates request
            swap = await db_create_shift_swap(
                org_id=org_id,
                requester_staff_id=req_id,
                target_staff_id=tgt_id,
                shift_date=date(2026, 6, 15),
                reason="Cita médica",
            )
            assert swap["status"] == "pending"
            swap_id = swap["id"]

            # Step 2: target accepts
            accepted = await db_update_swap_status(
                org_id, swap_id, "target_accepted", decided_by=tgt_id
            )
            assert accepted["status"] == "target_accepted"

            # Step 3: admin approves
            approved = await db_update_swap_status(
                org_id, swap_id, "admin_approved", decided_by="admin"
            )
            assert approved["status"] == "admin_approved"

            # Step 4: execute swap — no actual shifts, so swapped=False but no exception
            result = await db_execute_swap(org_id, swap_id)
            assert result["swapped"] is False  # no staff_shifts on that date
            assert result["swap"]["status"] == "admin_approved"

            # Verify final state in DB
            final = await db_get_swap(org_id, swap_id)
            assert final["status"] == "admin_approved"
            assert final["decided_by"] == "admin"


@_SKIP_INTEGRATION
async def test_tenant_isolation_swap(db_conn):
    """A swap request created in org A is invisible to org B's scope."""
    org_a = await _insert_org(db_conn)
    org_b = await _insert_org(db_conn)

    req_a = await _insert_staff(db_conn, org_a, "AliceA")
    tgt_a = await _insert_staff(db_conn, org_a, "BobA")

    proxy = _ConnProxy(db_conn)
    shim  = _PoolShim(proxy)

    with patch("app.services.database.get_pool", AsyncMock(return_value=shim)):
        from app.repositories.staff_comms_repo import db_create_shift_swap, db_list_swaps_admin
        # Create swap in org A
        with tenant_scope(org_a):
            await db_create_shift_swap(
                org_id=org_a,
                requester_staff_id=req_a,
                target_staff_id=tgt_a,
                shift_date=date(2026, 6, 20),
            )

        # Org B should see zero swaps
        with tenant_scope(org_b):
            swaps_b = await db_list_swaps_admin(org_b)
        assert len(swaps_b) == 0, "Org B leaked org A's swap requests"

        # Org A should see exactly 1 swap
        with tenant_scope(org_a):
            swaps_a = await db_list_swaps_admin(org_a)
        assert len(swaps_a) == 1


@_SKIP_INTEGRATION
async def test_target_cannot_approve_admin_only(db_conn):
    """Target can only accept/reject. Admin-approve on a still-pending swap raises ValueError."""
    org_id = await _insert_org(db_conn)
    req_id = await _insert_staff(db_conn, org_id, "Req")
    tgt_id = await _insert_staff(db_conn, org_id, "Tgt")

    proxy = _ConnProxy(db_conn)
    shim  = _PoolShim(proxy)

    with patch("app.services.database.get_pool", AsyncMock(return_value=shim)):
        from app.repositories.staff_comms_repo import db_create_shift_swap, db_update_swap_status
        with tenant_scope(org_id):
            swap = await db_create_shift_swap(
                org_id=org_id,
                requester_staff_id=req_id,
                target_staff_id=tgt_id,
                shift_date=date(2026, 6, 25),
            )
            swap_id = swap["id"]

            # Attempt to admin-approve while still pending (target hasn't accepted yet)
            with pytest.raises(ValueError, match="Invalid transition"):
                await db_update_swap_status(
                    org_id, swap_id, "admin_approved", decided_by="admin"
                )
