"""
tests/test_reservation_reminders.py
====================================
Integration tests for the reservation reminder pipeline:
  - db_get_upcoming_unconfirmed window (24h)
  - db_get_upcoming_unconfirmed excludes already-sent reminders
  - db_get_upcoming_unconfirmed only returns 'confirmed' status
  - db_mark_confirmation_sent idempotency
  - db_get_pending_confirmation_for_phone returns most recent for phone
  - db_cancel_reservation sets cancelled_at + cancellation_reason
  - db_mark_no_show sets no_show=TRUE + status='no_show'

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Reuses the _ConnProxy / _PoolShim / _set_org_scope pattern from
    test_loyalty_redemption.py.
"""

import os
import uuid
from datetime import datetime, timedelta

import asyncpg
import pytest

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers (mirror tests/test_loyalty_redemption.py) ────────────────────────


class _ConnProxy:
    __slots__ = ("_c",)

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)

    async def execute(self, *a, **kw):
        return await object.__getattribute__(self, "_c").execute(*a, **kw)

    async def fetch(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetch(*a, **kw)

    async def fetchrow(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchrow(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchval(*a, **kw)

    def transaction(self, *a, **kw):
        return object.__getattribute__(self, "_c").transaction(*a, **kw)


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back connection with get_pool mocked."""
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        async def _fake_get_pool():
            return shim
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_id(db_conn):
    """Create a single test org and return its id."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "ReservationReminderTestOrg",
        f"resv-reminder-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org),
    )


def _date_time_offset(hours_from_now: int) -> tuple[str, str]:
    """Return (YYYY-MM-DD, HH:MI) strings for the given offset from NOW()."""
    moment = datetime.utcnow() + timedelta(hours=hours_from_now)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")


async def _seed_reservation(
    conn,
    org,
    *,
    phone: str = "573001000001",
    bot_number: str = "573009999999",
    name: str = "Test Customer",
    hours_from_now: int = 12,
    status: str = "confirmed",
    confirmation_sent: bool = False,
    guests: int = 2,
) -> int:
    """Seed a reservation row at the given offset from NOW(). Returns id."""
    date_s, time_s = _date_time_offset(hours_from_now)
    row = await conn.fetchrow(
        """INSERT INTO reservations
             (name, "date", "time", guests, phone, bot_number, status,
              confirmation_sent, org_id, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
           RETURNING id""",
        name, date_s, time_s, guests, phone, bot_number, status,
        confirmation_sent, org,
    )
    return row["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests for db_get_upcoming_unconfirmed
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_upcoming_unconfirmed_window(db_conn, org_id):
    """Insert 3 reservations at 3h, 12h, 30h ahead. Only the first 2 should
    appear in the 24h window. The 30h one is outside the window."""
    from app.repositories.reservations_repo import db_get_upcoming_unconfirmed
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    id_3h = await _seed_reservation(db_conn, org_id, phone="573001000010", hours_from_now=3)
    id_12h = await _seed_reservation(db_conn, org_id, phone="573001000011", hours_from_now=12)
    _id_30h = await _seed_reservation(db_conn, org_id, phone="573001000012", hours_from_now=30)

    with tenant_scope(org_id):
        upcoming = await db_get_upcoming_unconfirmed(hours_ahead=24)

    upcoming_ids = {r["id"] for r in upcoming}
    assert id_3h in upcoming_ids, "3h reservation should be in 24h window"
    assert id_12h in upcoming_ids, "12h reservation should be in 24h window"
    assert _id_30h not in upcoming_ids, "30h reservation should be outside 24h window"


@pytest.mark.asyncio
async def test_get_upcoming_unconfirmed_excludes_already_sent(db_conn, org_id):
    """Reservation with confirmation_sent=TRUE must not appear in the result."""
    from app.repositories.reservations_repo import db_get_upcoming_unconfirmed
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    id_pending = await _seed_reservation(
        db_conn, org_id, phone="573002000001", hours_from_now=10,
        confirmation_sent=False,
    )
    id_already_sent = await _seed_reservation(
        db_conn, org_id, phone="573002000002", hours_from_now=10,
        confirmation_sent=True,
    )

    with tenant_scope(org_id):
        upcoming = await db_get_upcoming_unconfirmed(hours_ahead=24)

    upcoming_ids = {r["id"] for r in upcoming}
    assert id_pending in upcoming_ids
    assert id_already_sent not in upcoming_ids, (
        "Already-sent reminder must be excluded so we don't spam customers"
    )


@pytest.mark.asyncio
async def test_get_upcoming_unconfirmed_excludes_unconfirmed_status(db_conn, org_id):
    """Only status='confirmed' rows should be returned (not 'pending', not 'cancelled')."""
    from app.repositories.reservations_repo import db_get_upcoming_unconfirmed
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    id_confirmed = await _seed_reservation(
        db_conn, org_id, phone="573003000001", hours_from_now=10, status="confirmed",
    )
    id_pending = await _seed_reservation(
        db_conn, org_id, phone="573003000002", hours_from_now=10, status="pending",
    )
    id_cancelled = await _seed_reservation(
        db_conn, org_id, phone="573003000003", hours_from_now=10, status="cancelled",
    )

    with tenant_scope(org_id):
        upcoming = await db_get_upcoming_unconfirmed(hours_ahead=24)

    upcoming_ids = {r["id"] for r in upcoming}
    assert id_confirmed in upcoming_ids
    assert id_pending not in upcoming_ids
    assert id_cancelled not in upcoming_ids


# ─────────────────────────────────────────────────────────────────────────────
# Tests for db_mark_confirmation_sent
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_confirmation_sent_idempotent(db_conn, org_id):
    """Marking confirmation_sent flips the flag and is idempotent across calls."""
    from app.repositories.reservations_repo import (
        db_mark_confirmation_sent,
        db_get_upcoming_unconfirmed,
    )
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    res_id = await _seed_reservation(
        db_conn, org_id, phone="573004000001", hours_from_now=8, confirmation_sent=False,
    )

    with tenant_scope(org_id):
        # First call: should set flag
        await db_mark_confirmation_sent(res_id)
        # Second call (idempotency check): no error, still flagged
        await db_mark_confirmation_sent(res_id)

    flag = await db_conn.fetchval(
        "SELECT confirmation_sent FROM reservations WHERE id=$1", res_id,
    )
    assert flag is True

    # And it's now excluded from upcoming
    with tenant_scope(org_id):
        upcoming = await db_get_upcoming_unconfirmed(hours_ahead=24)
    assert res_id not in {r["id"] for r in upcoming}


# ─────────────────────────────────────────────────────────────────────────────
# Tests for db_get_pending_confirmation_for_phone
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pending_confirmation_for_phone_returns_recent(db_conn, org_id):
    """Two reservations for the same phone, both with reminder sent: returns the
    most recent (by date DESC, time DESC). Older one is discarded."""
    from app.repositories.reservations_repo import db_get_pending_confirmation_for_phone
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    customer_phone = "573005000001"

    id_older = await _seed_reservation(
        db_conn, org_id, phone=customer_phone, hours_from_now=4,
        status="confirmed", confirmation_sent=True,
    )
    id_newer = await _seed_reservation(
        db_conn, org_id, phone=customer_phone, hours_from_now=20,
        status="confirmed", confirmation_sent=True,
    )

    with tenant_scope(org_id):
        pending = await db_get_pending_confirmation_for_phone(org_id, customer_phone)

    assert pending is not None
    assert pending["id"] == id_newer, (
        f"Should return the more-recent (later date/time) reservation. "
        f"Got id={pending['id']}, expected {id_newer} (older was {id_older})"
    )


@pytest.mark.asyncio
async def test_get_pending_confirmation_for_phone_filters_by_org(db_conn, org_id):
    """A reservation for a different org_id with the same phone must not leak."""
    from app.repositories.reservations_repo import db_get_pending_confirmation_for_phone
    from app.services.tenant_context import tenant_scope

    other_org_row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "OtherOrgForReservation", f"other-org-{uuid.uuid4().hex[:8]}",
    )
    other_org = other_org_row["id"]

    customer_phone = "573006000001"

    # Seed in OTHER org first
    await _set_scope(db_conn, other_org)
    other_id = await _seed_reservation(
        db_conn, other_org, phone=customer_phone, hours_from_now=10,
        status="confirmed", confirmation_sent=True,
    )

    # Now scope to OUR org and ensure we don't see other org's reservation
    await _set_scope(db_conn, org_id)
    with tenant_scope(org_id):
        pending = await db_get_pending_confirmation_for_phone(org_id, customer_phone)

    assert pending is None, (
        f"Should NOT return a reservation from a different org "
        f"(other_id={other_id})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for db_cancel_reservation and db_mark_no_show (status updates)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_reservation_sets_cancelled_at_and_reason(db_conn, org_id):
    """Cancelling sets status='cancelled', cancelled_at=NOW(), and the reason."""
    from app.repositories.reservations_repo import db_cancel_reservation
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    res_id = await _seed_reservation(
        db_conn, org_id, phone="573007000001", hours_from_now=10,
        status="confirmed",
    )

    with tenant_scope(org_id):
        result = await db_cancel_reservation(res_id, reason="customer_reminder_response")

    assert result is not None
    assert result["status"] == "cancelled"
    assert result["cancelled_at"] is not None
    assert result["cancellation_reason"] == "customer_reminder_response"


@pytest.mark.asyncio
async def test_mark_no_show_sets_flag_and_status(db_conn, org_id):
    """Marking no-show sets no_show=TRUE and status='no_show'."""
    from app.repositories.reservations_repo import db_mark_no_show
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    res_id = await _seed_reservation(
        db_conn, org_id, phone="573008000001", hours_from_now=-2,
        status="confirmed",
    )

    with tenant_scope(org_id):
        result = await db_mark_no_show(res_id)

    assert result is not None
    assert result["no_show"] is True
    assert result["status"] == "no_show"
