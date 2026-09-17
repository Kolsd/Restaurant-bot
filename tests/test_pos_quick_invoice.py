"""
tests/test_pos_quick_invoice.py
================================
Regression tests for POST /api/pos/quick-invoice (app/routes/tables.py,
pos_quick_invoice), which used to ALWAYS 500:

  ValueError: Cannot resolve org_id for table_order qi-xxxxxxxx (table_id=None)

Two root causes, both in the route:

  1. The `order` dict it builds had `table_id: None` and no `org_id` /
     `restaurant_id` key, so db_save_table_order (app/repositories/tables_repo.py)
     couldn't resolve the tenant and raised.
  2. `branch_id = body.branch_id or user.get("branch_id") or restaurant["id"]`
     fell back from a location id to an ORG id on the last `or` — the
     org_id/location_id conflation CLAUDE.md forbids. `users.branch_id` is
     itself ambiguous (pre-migration-0081 legacy column); the explicit field
     is `user.get("location_id")`.

A third, same-shape bug was found while reviewing the payment/finalize call
right after check creation: db_create_checks always creates a check in
status='open', but db_finalize_check_payment only commits a check that is
in status='paying' (see db_claim_check_for_payment's docstring) and
silently returns False otherwise. The route used to call finalize directly
without claiming first and ignored the return value — it would have
reported {"success": true} while the check stayed 'open'/unpaid forever.

These tests hit the route HANDLER directly (not through TestClient) and
verify the actual rows written in a real (rolled-back) test-DB transaction.

Reason for calling the handler directly instead of TestClient: pytest-asyncio
fixtures hold an asyncpg connection bound to one event loop; FastAPI's
TestClient spins up a separate loop, and the lifespan pool from the app is
unaware of the test's transaction. This is the validated pattern from
tests/test_manual_payment_proof.py.

Requirements: TEST_DATABASE_URL must be set; otherwise the whole module is
skipped.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers (validated _ConnProxy / _PoolShim pattern, see docs/claude/testing.md) ──


class _ConnProxy:
    """Thin asyncpg-compatible proxy — works around Connection.__slots__."""

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


async def _set_scope(conn, org_id):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


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
            # CRITICAL: without this, postgres (superuser) bypasses FORCE RLS
            # and the tenant-scoped INSERTs below would "pass" without
            # exercising the real WITH CHECK policy that caught this bug.
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_with_location(db_conn):
    """Create a fresh org + one location and return (org_id, location_id)."""
    suffix = uuid.uuid4().hex[:8]
    org_id = await db_conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        f"QuickInvoiceTestOrg_{suffix}",
        f"quick-invoice-org-{suffix}",
    )
    location_id = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, code) VALUES ($1, $2, $3) RETURNING id",
        org_id, "Sede principal", f"qi-sede-{suffix}",
    )
    return org_id, location_id


def _restaurant_dict(org_id: int, location_id: int | None = None) -> dict:
    return {
        "id": org_id,
        "org_id": org_id,
        "location_id": location_id,
        "name": "QuickInvoiceTest",
        "whatsapp_number": "+573000000000",
        "features": {},
    }


class _StubRequest:
    """pos_quick_invoice only threads `request` through to get_current_restaurant
    / get_current_user, both of which are monkeypatched below — no real
    Request object needed."""
    pass


def _patch_deps(monkeypatch, restaurant: dict, user: dict):
    from app.routes import tables as tables_mod
    monkeypatch.setattr(tables_mod, "get_current_restaurant", AsyncMock(return_value=restaurant))
    monkeypatch.setattr(tables_mod, "get_current_user", AsyncMock(return_value=user))


def _body(**overrides):
    from app.routes.tables import QuickInvoiceBody, QuickInvoiceItem
    payload = {
        "items": [{"name": "Almuerzo ejecutivo", "qty": 2, "unit_price": 15000.0}],
        "tip_amount": 0.0,
        "payment_method": "efectivo",
        "customer_name": "Consumidor Final",
        "customer_nit": "222222222",
        "customer_email": "",
        "order_type": "salon",
        "table_name": "Caja",
    }
    payload.update(overrides)
    return QuickInvoiceBody(**payload)


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_quick_invoice_writes_org_id_and_null_branch_when_no_location(
    db_conn, org_with_location, monkeypatch,
):
    """
    Regression for the 500: no body.branch_id, no user location — this is
    exactly the request shape that used to raise ValueError. Must now
    succeed AND write a table_orders row with the caller's org_id, the
    correct total, and branch_id left NULL (never guessed from org_id).
    """
    from app.routes import tables as tables_mod

    org_id, _location_id = org_with_location
    await _set_scope(db_conn, org_id)

    restaurant = _restaurant_dict(org_id, location_id=None)
    user = {"username": "caja@test", "role": "caja", "org_id": org_id, "location_id": None}
    _patch_deps(monkeypatch, restaurant, user)

    body = _body()  # 2 x $15000 = $30000, no tip
    result = await tables_mod.pos_quick_invoice(_StubRequest(), body)

    assert result["success"] is True
    assert result["total"] == pytest.approx(30000.0)
    order_id = result["order_id"]
    check_id = result["check_id"]

    row = await db_conn.fetchrow(
        "SELECT org_id, branch_id, total, table_id, notes FROM table_orders WHERE id=$1",
        order_id,
    )
    assert row is not None, "quick-invoice must actually write a table_orders row"
    assert row["org_id"] == org_id
    assert row["branch_id"] is None, (
        "with no explicit location, branch_id must stay NULL — never the org_id"
    )
    assert float(row["total"]) == pytest.approx(30000.0)
    # table_orders.table_id is NOT NULL with no FK to restaurant_tables — the
    # route reuses the synthetic order_id (never collides with a real table
    # id, which looks like "table-{org_id}-{number}").
    assert row["table_id"] == order_id

    check_row = await db_conn.fetchrow(
        "SELECT base_order_id, status, total, payments FROM table_checks WHERE id=$1",
        check_id,
    )
    assert check_row is not None, "quick-invoice must create the check row"
    assert check_row["base_order_id"] == order_id
    assert float(check_row["total"]) == pytest.approx(30000.0)


@pytest.mark.asyncio
async def test_quick_invoice_uses_real_location_never_org_id(
    db_conn, org_with_location, monkeypatch,
):
    """
    When the caller (or the user's own record) carries a real location_id,
    branch_id must be THAT location — and must never be substituted by the
    org id, even though `restaurant["id"] == org_id` was sitting right there
    as the old (wrong) fallback.
    """
    from app.routes import tables as tables_mod

    org_id, location_id = org_with_location
    assert location_id != org_id, "fixture sanity: ids must differ to prove no conflation"
    await _set_scope(db_conn, org_id)

    restaurant = _restaurant_dict(org_id, location_id=location_id)
    user = {"username": "caja@test", "role": "caja", "org_id": org_id, "location_id": location_id}
    _patch_deps(monkeypatch, restaurant, user)

    body = _body()
    result = await tables_mod.pos_quick_invoice(_StubRequest(), body)
    assert result["success"] is True

    row = await db_conn.fetchrow(
        "SELECT org_id, branch_id FROM table_orders WHERE id=$1", result["order_id"],
    )
    assert row["org_id"] == org_id
    assert row["branch_id"] == location_id
    assert row["branch_id"] != org_id


@pytest.mark.asyncio
async def test_quick_invoice_body_branch_id_overrides_user_location(
    db_conn, org_with_location, monkeypatch,
):
    """An explicit body.branch_id (e.g. multi-sede caja picking a sede in the
    UI) takes priority over the user's own location_id, and is still never
    the org id."""
    from app.routes import tables as tables_mod

    org_id, location_id = org_with_location
    await _set_scope(db_conn, org_id)

    # A second real location of the SAME org, distinct from the user's own.
    other_location_id = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, code) VALUES ($1, $2, $3) RETURNING id",
        org_id, "Sede 2", f"qi-sede2-{uuid.uuid4().hex[:8]}",
    )

    restaurant = _restaurant_dict(org_id, location_id=location_id)
    user = {"username": "caja@test", "role": "caja", "org_id": org_id, "location_id": location_id}
    _patch_deps(monkeypatch, restaurant, user)

    body = _body(branch_id=other_location_id)
    result = await tables_mod.pos_quick_invoice(_StubRequest(), body)
    assert result["success"] is True

    row = await db_conn.fetchrow(
        "SELECT branch_id FROM table_orders WHERE id=$1", result["order_id"],
    )
    assert row["branch_id"] == other_location_id
    assert row["branch_id"] != org_id


@pytest.mark.asyncio
async def test_quick_invoice_domicilio_order_type(
    db_conn, org_with_location, monkeypatch,
):
    """order_type='domicilio' must also succeed and write org_id/total correctly
    (this path shares the exact same order-dict construction as 'salon')."""
    from app.routes import tables as tables_mod

    org_id, location_id = org_with_location
    await _set_scope(db_conn, org_id)

    restaurant = _restaurant_dict(org_id, location_id=location_id)
    user = {"username": "caja@test", "role": "caja", "org_id": org_id, "location_id": location_id}
    _patch_deps(monkeypatch, restaurant, user)

    body = _body(order_type="domicilio", tip_amount=1000.0)
    result = await tables_mod.pos_quick_invoice(_StubRequest(), body)

    assert result["success"] is True
    assert result["total"] == pytest.approx(31000.0)  # 30000 + 1000 tip

    row = await db_conn.fetchrow(
        "SELECT org_id, branch_id, total, notes FROM table_orders WHERE id=$1",
        result["order_id"],
    )
    assert row["org_id"] == org_id
    assert row["branch_id"] == location_id
    assert row["branch_id"] != org_id
    assert float(row["total"]) == pytest.approx(31000.0)
    assert "domicilio" in row["notes"]


@pytest.mark.asyncio
async def test_quick_invoice_check_is_actually_finalized_not_left_open(
    db_conn, org_with_location, monkeypatch,
):
    """
    Same-shape bug as the org_id one: db_create_checks always creates
    status='open'; db_finalize_check_payment only commits a check whose
    status is 'paying' and silently no-ops otherwise. The route used to call
    finalize directly (no claim) and ignore the return value, so it would
    report success while the check stayed 'open' and unpaid.

    After the fix (claim-then-finalize, like pay_check()), the check must
    actually land in status='invoiced' with the payment recorded.
    """
    from app.routes import tables as tables_mod

    org_id, location_id = org_with_location
    await _set_scope(db_conn, org_id)

    restaurant = _restaurant_dict(org_id, location_id=location_id)
    user = {"username": "caja@test", "role": "caja", "org_id": org_id, "location_id": location_id}
    _patch_deps(monkeypatch, restaurant, user)

    body = _body(payment_method="tarjeta")
    result = await tables_mod.pos_quick_invoice(_StubRequest(), body)
    assert result["success"] is True

    check_row = await db_conn.fetchrow(
        "SELECT status, payments, total FROM table_checks WHERE id=$1", result["check_id"],
    )
    assert check_row["status"] == "invoiced", (
        "quick-invoice claims success but the check was left 'open'/unpaid — "
        "the finalize call must actually commit, not silently no-op"
    )
    import json as _json
    payments = check_row["payments"]
    if isinstance(payments, str):
        payments = _json.loads(payments)
    assert payments and payments[0]["method"] == "tarjeta"
    assert float(payments[0]["amount"]) == pytest.approx(30000.0)

    # The table_orders row must also reflect the check being fully settled.
    order_row = await db_conn.fetchrow(
        "SELECT status FROM table_orders WHERE id=$1", result["order_id"],
    )
    assert order_row["status"] == "factura_entregada"


@pytest.mark.asyncio
async def test_quick_invoice_tenant_isolation_between_orgs(
    db_conn, org_with_location, monkeypatch,
):
    """Two quick invoices from two different orgs must never cross-write —
    each table_orders row keeps its own org_id, and org A cannot see org B's
    row while scoped to A."""
    from app.routes import tables as tables_mod

    org_a, loc_a = org_with_location
    suffix_b = uuid.uuid4().hex[:8]

    await _set_scope(db_conn, org_a)
    org_b = await db_conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        f"QuickInvoiceTestOrgB_{suffix_b}", f"quick-invoice-org-b-{suffix_b}",
    )
    loc_b = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, code) VALUES ($1, $2, $3) RETURNING id",
        org_b, "Sede B", f"qi-sede-b-{suffix_b}",
    )

    # Order for org A.
    await _set_scope(db_conn, org_a)
    _patch_deps(
        monkeypatch,
        _restaurant_dict(org_a, location_id=loc_a),
        {"username": "a@test", "role": "caja", "org_id": org_a, "location_id": loc_a},
    )
    result_a = await tables_mod.pos_quick_invoice(_StubRequest(), _body())

    # Order for org B.
    await _set_scope(db_conn, org_b)
    _patch_deps(
        monkeypatch,
        _restaurant_dict(org_b, location_id=loc_b),
        {"username": "b@test", "role": "caja", "org_id": org_b, "location_id": loc_b},
    )
    result_b = await tables_mod.pos_quick_invoice(_StubRequest(), _body())

    await _set_scope(db_conn, org_a)
    row_a = await db_conn.fetchrow(
        "SELECT org_id, branch_id FROM table_orders WHERE id=$1", result_a["order_id"],
    )
    assert row_a["org_id"] == org_a
    assert row_a["branch_id"] == loc_a

    await _set_scope(db_conn, org_b)
    row_b = await db_conn.fetchrow(
        "SELECT org_id, branch_id FROM table_orders WHERE id=$1", result_b["order_id"],
    )
    assert row_b["org_id"] == org_b
    assert row_b["branch_id"] == loc_b
    assert row_b["org_id"] != row_a["org_id"]
