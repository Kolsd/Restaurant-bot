"""
tests/test_loyalty_redemption.py
=================================
Integration tests for the loyalty point redemption pipeline:
  - db_redeem_loyalty_points (existed) — happy path, insufficient balance,
    invalid input, concurrent FOR UPDATE locking.
  - db_apply_redemption_to_order — annotates orders rows for caja.
  - db_apply_redemption_to_table_check — annotates table_checks rows.
  - agent.execute_action (action='redeem_loyalty') tool integration —
    end-to-end redemption flow exercised through the action dispatcher.

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Reuses the _ConnProxy / _PoolShim / _set_org_scope pattern from
    test_loyalty_aggregates.py — that fixture is the validated reference
    for integration tests post-Wave-2.
"""

import asyncio
import os
import uuid

import asyncpg
import pytest

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers (mirror tests/test_loyalty_aggregates.py) ────────────────────────


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
    """Yield a rolled-back connection with get_pool mocked (ref. test_loyalty_aggregates.py)."""
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
        "LoyaltyRedemptionTestOrg", f"loyalty-redem-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org),
    )


async def _seed_loyalty_customer(conn, org, phone: str, balance: int):
    await conn.execute(
        """INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed)
           VALUES ($1, $2, $3, $3, 0)""",
        org, phone, balance,
    )


# ── Tests: db_redeem_loyalty_points happy path ──────────────────────────────


@pytest.mark.asyncio
async def test_redeem_happy_path(db_conn, org_id):
    """Customer with balance 200 → redeem 100 → balance 100, ledger row created, cop_discount correct."""
    from app.repositories.loyalty_repo import db_redeem_loyalty_points
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000001"
    await _seed_loyalty_customer(db_conn, org_id, phone, 200)

    with tenant_scope(org_id):
        result = await db_redeem_loyalty_points(org_id, phone, 100, order_id="ord-redeem-1")

    assert result["redeemed"] == 100
    # Default points_per_1k=1, point_value_cop=10 (from _loyalty_cfg)
    # but features may override; just check it's a positive int derived from cfg
    assert isinstance(result["cop_discount"], int)
    assert result["cop_discount"] > 0
    assert result["new_balance"] == 100

    # Ledger row was inserted
    ledger_row = await db_conn.fetchrow(
        "SELECT delta, reason, order_id FROM loyalty_ledger "
        "WHERE org_id=$1 AND phone=$2 AND reason='redeem'",
        org_id, phone,
    )
    assert ledger_row is not None
    assert ledger_row["delta"] == -100
    assert ledger_row["order_id"] == "ord-redeem-1"

    # loyalty_customers updated
    bal_row = await db_conn.fetchrow(
        "SELECT points_balance, total_redeemed FROM loyalty_customers "
        "WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal_row["points_balance"] == 100
    assert bal_row["total_redeemed"] == 100


# ── Tests: insufficient balance ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_insufficient_balance(db_conn, org_id):
    """Customer balance 50, attempt redeem 100 → ValueError (no DB writes)."""
    from app.repositories.loyalty_repo import db_redeem_loyalty_points
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000002"
    await _seed_loyalty_customer(db_conn, org_id, phone, 50)

    with tenant_scope(org_id):
        with pytest.raises(ValueError) as excinfo:
            await db_redeem_loyalty_points(org_id, phone, 100, order_id="ord-fail-1")

    assert "Saldo insuficiente" in str(excinfo.value)

    # Verify nothing changed: balance still 50, no redeem ledger row.
    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 50

    redeem_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM loyalty_ledger WHERE org_id=$1 AND phone=$2 AND reason='redeem'",
        org_id, phone,
    )
    assert redeem_count == 0


# ── Tests: zero / negative points rejected ────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_zero_points_raises(db_conn, org_id):
    """Redeeming 0 or negative points raises ValueError before touching the DB."""
    from app.repositories.loyalty_repo import db_redeem_loyalty_points
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000003"
    await _seed_loyalty_customer(db_conn, org_id, phone, 100)

    with tenant_scope(org_id):
        with pytest.raises(ValueError):
            await db_redeem_loyalty_points(org_id, phone, 0, order_id="ord-zero")

        with pytest.raises(ValueError):
            await db_redeem_loyalty_points(org_id, phone, -5, order_id="ord-neg")

    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 100


# ── Tests: concurrent redemption (FOR UPDATE locking) ────────────────────────


@pytest.mark.asyncio
async def test_redeem_concurrent_only_one_wins(raw_pool, monkeypatch):
    """Two concurrent calls each trying to redeem 80 from a balance of 100 — only one can succeed.

    Uses two SEPARATE pool connections (real concurrency) instead of the
    single rolled-back connection of the db_conn fixture.  The test seeds a
    fresh org + customer in its own setup connection, runs the two redeems
    in parallel, then asserts exactly one ValueError + final balance 20.
    The test cleans up its own data at the end so we don't leave residue
    in the test DB.
    """
    from app.repositories.loyalty_repo import db_redeem_loyalty_points
    from app.services.tenant_context import tenant_scope
    from app.services import database as db_module

    # Seed via a setup connection (non-rolled-back so concurrent connections see the row).
    setup_conn = await raw_pool.acquire()
    org_uuid_short = uuid.uuid4().hex[:8]
    phone = f"3001{uuid.uuid4().hex[:6]}"
    setup_org_id = None
    try:
        setup_org_id = await setup_conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            f"ConcurrencyTestOrg_{org_uuid_short}",
            f"loyalty-concurrent-{org_uuid_short}",
        )
        await setup_conn.execute(
            """INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed)
               VALUES ($1, $2, 100, 100, 0)""",
            setup_org_id, phone,
        )
    finally:
        await raw_pool.release(setup_conn)

    # Build a pool shim that hands out a NEW connection on every acquire(),
    # so two parallel coroutines actually use distinct connections (FOR
    # UPDATE only blocks across distinct connections).
    class _RealPoolShim:
        def __init__(self, real_pool):
            self._pool = real_pool

        def acquire(self):
            return _RealAcquireCtx(self._pool)

        async def close(self):
            pass

    class _RealAcquireCtx:
        def __init__(self, real_pool):
            self._pool = real_pool
            self._conn = None

        async def __aenter__(self):
            self._conn = await self._pool.acquire()
            # Drop superuser so RLS applies, scope to the test org for this acquire.
            await self._conn.execute("SET ROLE mesio_app")
            await self._conn.execute(
                "SELECT set_config('app.org_id', $1::text, false)",
                str(setup_org_id),
            )
            return _ConnProxy(self._conn)

        async def __aexit__(self, *_):
            try:
                await self._conn.execute("RESET ROLE")
            except Exception:
                pass
            await self._pool.release(self._conn)
            self._conn = None

    async def _fake_get_pool():
        return _RealPoolShim(raw_pool)
    monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

    async def _attempt():
        with tenant_scope(setup_org_id):
            try:
                return await db_redeem_loyalty_points(
                    setup_org_id, phone, 80, order_id=f"concurrent-{uuid.uuid4().hex[:6]}"
                )
            except ValueError as e:
                return e

    try:
        results = await asyncio.gather(_attempt(), _attempt())

        successes = [r for r in results if isinstance(r, dict)]
        failures  = [r for r in results if isinstance(r, ValueError)]
        # Exactly one redeem must succeed; the other must see insufficient balance.
        assert len(successes) == 1, f"Expected 1 success, got {results}"
        assert len(failures) == 1, f"Expected 1 ValueError, got {results}"

        # Final balance: 100 - 80 = 20
        cleanup_conn = await raw_pool.acquire()
        try:
            await cleanup_conn.execute("SET ROLE mesio_app")
            await cleanup_conn.execute(
                "SELECT set_config('app.org_id', $1::text, false)",
                str(setup_org_id),
            )
            final = await cleanup_conn.fetchval(
                "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
                setup_org_id, phone,
            )
            await cleanup_conn.execute("RESET ROLE")
            assert final == 20
        finally:
            await raw_pool.release(cleanup_conn)
    finally:
        # Always clean up the org (cascades to children via ON DELETE CASCADE FKs)
        cleanup_conn = await raw_pool.acquire()
        try:
            await cleanup_conn.execute(
                "DELETE FROM loyalty_ledger WHERE org_id = $1", setup_org_id,
            )
            await cleanup_conn.execute(
                "DELETE FROM loyalty_customers WHERE org_id = $1", setup_org_id,
            )
            await cleanup_conn.execute(
                "DELETE FROM organizations WHERE id = $1", setup_org_id,
            )
        finally:
            await raw_pool.release(cleanup_conn)


# ── Tests: db_apply_redemption_to_order (delivery/pickup) ────────────────────


@pytest.mark.asyncio
async def test_apply_redemption_to_order_updates_columns(db_conn, org_id):
    """After applying a redemption, orders.loyalty_redeemed_points and loyalty_discount_cop reflect it."""
    from app.repositories.loyalty_repo import db_apply_redemption_to_order
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    order_id = str(uuid.uuid4())
    await db_conn.execute(
        """INSERT INTO orders
           (id, org_id, phone, order_type, status, paid, total, subtotal, items, bot_number)
           VALUES ($1, $2, '3009000001', 'domicilio', 'pendiente', false, 50000, 50000, '[]'::jsonb, 'bot-test')""",
        order_id, org_id,
    )

    with tenant_scope(org_id):
        ok = await db_apply_redemption_to_order(order_id, 50, 500)
    assert ok is True

    row = await db_conn.fetchrow(
        "SELECT loyalty_redeemed_points, loyalty_discount_cop FROM orders WHERE id=$1",
        order_id,
    )
    assert row["loyalty_redeemed_points"] == 50
    # NUMERIC column → asyncpg returns Decimal
    assert int(row["loyalty_discount_cop"]) == 500


@pytest.mark.asyncio
async def test_apply_redemption_to_order_not_found(db_conn, org_id):
    """If order_id doesn't exist in tenant scope, function returns False (no exception)."""
    from app.repositories.loyalty_repo import db_apply_redemption_to_order
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)

    with tenant_scope(org_id):
        ok = await db_apply_redemption_to_order("ord-does-not-exist", 10, 100)
    assert ok is False


@pytest.mark.asyncio
async def test_apply_redemption_to_order_rejects_zero_or_negative(db_conn, org_id):
    """Validation: points must be positive, cop_discount cannot be negative."""
    from app.repositories.loyalty_repo import db_apply_redemption_to_order
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)

    with tenant_scope(org_id):
        with pytest.raises(ValueError):
            await db_apply_redemption_to_order("ord-x", 0, 100)
        with pytest.raises(ValueError):
            await db_apply_redemption_to_order("ord-x", 10, -5)


# ── Tests: db_apply_redemption_to_table_check (dine-in) ──────────────────────


@pytest.mark.asyncio
async def test_apply_redemption_to_table_check_updates_columns(db_conn, org_id):
    """After applying redemption, table_checks columns reflect it."""
    from app.repositories.loyalty_repo import db_apply_redemption_to_table_check
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)

    # table_checks references base_order_id → need a parent row in table_orders.
    # table_orders has FK to organizations (via org_id) and is itself RLS-protected,
    # so we seed it under the same scope.
    base_order_id = "BASE-" + uuid.uuid4().hex[:8].upper()
    await db_conn.execute(
        """INSERT INTO table_orders
           (id, org_id, table_id, table_name, phone, items, total, status, base_order_id, sub_number)
           VALUES ($1, $2, 't-1', 'Mesa 1', '3009000002', '[]'::jsonb, 30000, 'recibido', $1, 1)""",
        base_order_id, org_id,
    )

    check_id = "CHK-" + uuid.uuid4().hex[:8].upper()
    # table_checks has no direct org_id column — tenant scope flows via the
    # base_order_id FK to table_orders.
    await db_conn.execute(
        """INSERT INTO table_checks
           (id, base_order_id, check_number, items, subtotal, tax_amount, total, status)
           VALUES ($1, $2, 1, '[]'::jsonb, 30000, 0, 30000, 'open')""",
        check_id, base_order_id,
    )

    with tenant_scope(org_id):
        ok = await db_apply_redemption_to_table_check(check_id, 25, 250)
    assert ok is True

    row = await db_conn.fetchrow(
        "SELECT loyalty_redeemed_points, loyalty_discount_cop FROM table_checks WHERE id=$1",
        check_id,
    )
    assert row["loyalty_redeemed_points"] == 25
    assert int(row["loyalty_discount_cop"]) == 250


# ── Tests: agent.execute_action redeem_loyalty integration ────────────────────


@pytest.mark.asyncio
async def test_agent_redeem_loyalty_external_happy_path(db_conn, org_id):
    """End-to-end: parsed={action:redeem_loyalty, points:50} → balance decremented + order annotated.

    Exercises agent._execute_redeem_loyalty for the external path:
    seeds a pending delivery order + a loyalty customer, asks agent to
    apply 50 points, asserts both ledger and order columns are updated.
    """
    from app.services.agent import _execute_redeem_loyalty
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000050"
    bot_number = "bot-redeem-ext"
    await _seed_loyalty_customer(db_conn, org_id, phone, 200)

    order_id = str(uuid.uuid4())
    await db_conn.execute(
        """INSERT INTO orders
           (id, org_id, phone, order_type, status, paid, total, subtotal, items, bot_number)
           VALUES ($1, $2, $3, 'domicilio', 'pendiente', false, 80000, 80000, '[]'::jsonb, $4)""",
        order_id, org_id, phone, bot_number,
    )

    parsed = {"action": "redeem_loyalty", "points": 50}
    restaurant_obj = {"id": org_id}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, phone, bot_number,
            table_context=None,
            restaurant_obj=restaurant_obj,
        )

    # Reply mentions points + COP discount (format-specific assertions)
    assert "50 puntos" in reply
    assert "150 puntos" in reply  # new balance = 200 - 50

    # Ledger has redeem entry
    redeem_row = await db_conn.fetchrow(
        "SELECT delta FROM loyalty_ledger "
        "WHERE org_id=$1 AND phone=$2 AND reason='redeem'",
        org_id, phone,
    )
    assert redeem_row is not None
    assert redeem_row["delta"] == -50

    # orders row annotated
    order_row = await db_conn.fetchrow(
        "SELECT loyalty_redeemed_points, loyalty_discount_cop FROM orders WHERE id=$1",
        order_id,
    )
    assert order_row["loyalty_redeemed_points"] == 50
    assert int(order_row["loyalty_discount_cop"]) > 0


@pytest.mark.asyncio
async def test_agent_redeem_loyalty_external_no_pending_order(db_conn, org_id):
    """If no pending delivery/pickup order exists, agent does NOT touch the balance."""
    from app.services.agent import _execute_redeem_loyalty
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000051"
    bot_number = "bot-redeem-noorder"
    await _seed_loyalty_customer(db_conn, org_id, phone, 100)

    parsed = {"action": "redeem_loyalty", "points": 50}
    restaurant_obj = {"id": org_id}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, phone, bot_number,
            table_context=None,
            restaurant_obj=restaurant_obj,
        )

    # Friendly fallback, no DB mutation
    assert reply  # non-empty
    assert "no encontr" in reply.lower() or "no tenés" in reply.lower() or "no tenes" in reply.lower()

    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 100  # untouched


@pytest.mark.asyncio
async def test_agent_redeem_loyalty_insufficient_balance_returns_friendly_message(db_conn, org_id):
    """When balance < requested points, agent returns a friendly message and does NOT decrement."""
    from app.services.agent import _execute_redeem_loyalty
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000052"
    bot_number = "bot-redeem-insuf"
    await _seed_loyalty_customer(db_conn, org_id, phone, 30)

    order_id = str(uuid.uuid4())
    await db_conn.execute(
        """INSERT INTO orders
           (id, org_id, phone, order_type, status, paid, total, subtotal, items, bot_number)
           VALUES ($1, $2, $3, 'recoger', 'pendiente', false, 50000, 50000, '[]'::jsonb, $4)""",
        order_id, org_id, phone, bot_number,
    )

    parsed = {"action": "redeem_loyalty", "points": 100}
    restaurant_obj = {"id": org_id}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, phone, bot_number,
            table_context=None,
            restaurant_obj=restaurant_obj,
        )

    # Friendly insufficient-balance message that includes both numbers.
    assert "30" in reply
    assert "100" in reply

    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 30  # unchanged


@pytest.mark.asyncio
async def test_agent_redeem_loyalty_salon_no_open_check(db_conn, org_id):
    """Salon: if customer hasn't asked for la cuenta yet, redemption is refused."""
    from app.services.agent import _execute_redeem_loyalty
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000060"
    await _seed_loyalty_customer(db_conn, org_id, phone, 100)

    # Active table_session + table_orders but NO table_checks (pre-checkout state)
    await db_conn.execute(
        """INSERT INTO table_sessions (table_id, table_name, phone, bot_number, status, org_id)
           VALUES ('t-99', 'Mesa 99', $2, 'bot-test', 'active', $1)""",
        org_id, phone,
    )
    base_order_id = "BASE-" + uuid.uuid4().hex[:8].upper()
    await db_conn.execute(
        """INSERT INTO table_orders
           (id, org_id, table_id, table_name, phone, items, total, status, base_order_id, sub_number)
           VALUES ($1, $2, 't-99', 'Mesa 99', $3, '[]'::jsonb, 30000, 'recibido', $1, 1)""",
        base_order_id, org_id, phone,
    )

    parsed = {"action": "redeem_loyalty", "points": 50}
    restaurant_obj = {"id": org_id}
    table_context = {"id": "t-99", "name": "Mesa 99"}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, phone, "bot-test",
            table_context=table_context,
            restaurant_obj=restaurant_obj,
        )

    # Refusal message + balance unchanged
    assert "cuenta abierta" in reply.lower() or "no tenés" in reply.lower() or "no tenes" in reply.lower()

    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 100  # untouched


@pytest.mark.asyncio
async def test_agent_redeem_loyalty_salon_with_open_check(db_conn, org_id):
    """Salon: with an open table_check, redemption is applied to balance + check columns."""
    from app.services.agent import _execute_redeem_loyalty
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000061"
    await _seed_loyalty_customer(db_conn, org_id, phone, 200)

    await db_conn.execute(
        """INSERT INTO table_sessions (table_id, table_name, phone, bot_number, status, org_id)
           VALUES ('t-100', 'Mesa 100', $2, 'bot-test', 'active', $1)""",
        org_id, phone,
    )
    base_order_id = "BASE-" + uuid.uuid4().hex[:8].upper()
    await db_conn.execute(
        """INSERT INTO table_orders
           (id, org_id, table_id, table_name, phone, items, total, status, base_order_id, sub_number)
           VALUES ($1, $2, 't-100', 'Mesa 100', $3, '[]'::jsonb, 60000, 'recibido', $1, 1)""",
        base_order_id, org_id, phone,
    )
    check_id = "CHK-" + uuid.uuid4().hex[:8].upper()
    await db_conn.execute(
        """INSERT INTO table_checks
           (id, base_order_id, check_number, items, subtotal, tax_amount, total, status)
           VALUES ($1, $2, 1, '[]'::jsonb, 60000, 0, 60000, 'open')""",
        check_id, base_order_id,
    )

    parsed = {"action": "redeem_loyalty", "points": 75}
    restaurant_obj = {"id": org_id}
    table_context = {"id": "t-100", "name": "Mesa 100"}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, phone, "bot-test",
            table_context=table_context,
            restaurant_obj=restaurant_obj,
        )

    # Reply confirms 75 points redeemed, new balance 125
    assert "75 puntos" in reply
    assert "125 puntos" in reply

    # Balance decremented
    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 125

    # table_check annotated
    chk_row = await db_conn.fetchrow(
        "SELECT loyalty_redeemed_points, loyalty_discount_cop FROM table_checks WHERE id=$1",
        check_id,
    )
    assert chk_row["loyalty_redeemed_points"] == 75
    assert int(chk_row["loyalty_discount_cop"]) > 0


@pytest.mark.asyncio
async def test_agent_redeem_loyalty_zero_points_rejected(db_conn, org_id):
    """0 or negative points → friendly rejection, no DB write."""
    from app.services.agent import _execute_redeem_loyalty
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000053"
    await _seed_loyalty_customer(db_conn, org_id, phone, 50)

    parsed = {"action": "redeem_loyalty", "points": 0}
    restaurant_obj = {"id": org_id}

    with tenant_scope(org_id):
        reply = await _execute_redeem_loyalty(
            parsed, phone, "bot-x",
            table_context=None,
            restaurant_obj=restaurant_obj,
        )

    assert "1 punto" in reply or "al menos" in reply

    bal = await db_conn.fetchval(
        "SELECT points_balance FROM loyalty_customers WHERE org_id=$1 AND phone=$2",
        org_id, phone,
    )
    assert bal == 50
