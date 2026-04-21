"""
tests/test_loyalty_campaigns.py
================================
Integration tests for the loyalty campaigns repository and endpoints.

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Uses _ConnProxy / _PoolShim pattern for asyncpg Connection __slots__
  - Every call wrapped in tenant_scope(org_id) per RLS rules
  - Validates state machine, CRUD, and tenant isolation

Test matrix
-----------
  test_create_and_get_campaign       — create returns draft, get retrieves it
  test_update_campaign_name          — PATCH name updates record
  test_delete_campaign               — DELETE returns True, second delete returns False
  test_list_campaigns_filter_status  — filter by status returns correct subset
  test_toggle_draft_to_active        — draft → active allowed
  test_toggle_active_to_paused       — active → paused allowed
  test_toggle_invalid_draft_to_paused — draft → paused raises ValueError (409)
  test_tenant_isolation_campaigns    — org A campaign invisible to org B
  test_no_scope_raises               — calling without tenant_scope raises TenantNotSetError
"""

import os
import pytest
import asyncpg

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)

# ── Helpers ───────────────────────────────────────────────────────────────────


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
    # Function-scoped: each test gets its own pool on its own event loop.
    # Module/session scope crashes with "Future attached to a different loop".
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    # tenant_db.tenant_connection() imports get_pool lazily from
    # app.services.database, so patching the source module is sufficient.
    # Do NOT patch app.services.tenant_db.get_pool (attribute doesn't exist).
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        # get_pool is async in app.services.database; mock must be too.
        async def _fake_get_pool():
            return shim
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        # Manual transaction so we can rollback in teardown WITHOUT raising
        # an exception (which pytest counts as a test error).
        tx = conn.transaction()
        await tx.start()
        try:
            # Drop superuser privilege so RLS actually enforces (test pool
            # connects as postgres; production app runs as mesio_app).
            # Without this, tenant_isolation policies are bypassed.
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_ids(db_conn):
    row_a = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "CampaignTestOrgA", "campaign-test-org-a",
    )
    row_b = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "CampaignTestOrgB", "campaign-test-org-b",
    )
    return row_a["id"], row_b["id"]


async def _set_scope(conn, org_id):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


# ── CRUD tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_campaign(db_conn, org_ids):
    """Create returns a draft campaign; get retrieves the same record."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_get_campaign
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        created = await db_create_campaign(
            org_id=org_a,
            name="Test Campaign",
            description="A test campaign",
            segment="vip-active",
            trigger_type="manual",
            message_template="Hola {{nombre}}, tienes una oferta especial.",
        )

    assert created["status"] == "draft"
    assert created["name"] == "Test Campaign"
    assert created["trigger_type"] == "manual"
    assert created["segment"] == "vip-active"
    camp_id = created["id"]

    with tenant_scope(org_a):
        fetched = await db_get_campaign(camp_id)

    assert fetched is not None
    assert fetched["id"] == camp_id
    assert fetched["name"] == "Test Campaign"


@pytest.mark.asyncio
async def test_update_campaign_name(db_conn, org_ids):
    """PATCH name field is reflected in the updated record."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_update_campaign
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        created = await db_create_campaign(
            org_id=org_a,
            name="Original Name",
            trigger_type="birthday",
            message_template="Feliz cumpleaños {{nombre}}!",
        )
        camp_id = created["id"]
        updated = await db_update_campaign(camp_id, {"name": "Updated Name"})

    assert updated is not None
    assert updated["name"] == "Updated Name"
    assert updated["trigger_type"] == "birthday"  # unchanged


@pytest.mark.asyncio
async def test_delete_campaign(db_conn, org_ids):
    """Delete returns True on first call, False on second (not found)."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_delete_campaign
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        created = await db_create_campaign(
            org_id=org_a,
            name="To Delete",
            trigger_type="manual",
            message_template="Bye",
        )
        camp_id = created["id"]
        first  = await db_delete_campaign(camp_id)
        second = await db_delete_campaign(camp_id)

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_list_campaigns_filter_status(db_conn, org_ids):
    """Filter by status returns only campaigns matching that status."""
    from app.repositories.loyalty_campaigns_repo import (
        db_create_campaign, db_list_campaigns, db_toggle_campaign_status
    )
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        c1 = await db_create_campaign(
            org_id=org_a, name="Draft Camp", trigger_type="manual", message_template="Draft msg"
        )
        c2 = await db_create_campaign(
            org_id=org_a, name="Active Camp", trigger_type="birthday", message_template="Active msg"
        )
        # Activate c2
        await db_toggle_campaign_status(c2["id"], "active")

        drafts  = await db_list_campaigns(status="draft")
        actives = await db_list_campaigns(status="active")

    draft_ids  = [c["id"] for c in drafts]
    active_ids = [c["id"] for c in actives]

    assert c1["id"] in draft_ids
    assert c2["id"] not in draft_ids
    assert c2["id"] in active_ids
    assert c1["id"] not in active_ids


# ── State machine tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_draft_to_active(db_conn, org_ids):
    """draft → active is a valid transition."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_toggle_campaign_status
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        camp = await db_create_campaign(
            org_id=org_a, name="Activate Me", trigger_type="manual", message_template="Go!"
        )
        result = await db_toggle_campaign_status(camp["id"], "active")

    assert result is not None
    assert result["status"] == "active"


@pytest.mark.asyncio
async def test_toggle_active_to_paused(db_conn, org_ids):
    """active → paused is a valid transition."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_toggle_campaign_status
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        camp = await db_create_campaign(
            org_id=org_a, name="Pause Me", trigger_type="manual", message_template="..."
        )
        await db_toggle_campaign_status(camp["id"], "active")
        result = await db_toggle_campaign_status(camp["id"], "paused")

    assert result is not None
    assert result["status"] == "paused"


@pytest.mark.asyncio
async def test_toggle_invalid_draft_to_paused(db_conn, org_ids):
    """draft → paused is INVALID and raises ValueError (maps to 409 in routes)."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_toggle_campaign_status
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        camp = await db_create_campaign(
            org_id=org_a, name="Bad Transition", trigger_type="manual", message_template="..."
        )
        with pytest.raises(ValueError, match="Invalid transition"):
            await db_toggle_campaign_status(camp["id"], "paused")


# ── Tenant isolation test ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_isolation_campaigns(db_conn, org_ids):
    """Campaign created in org A is invisible when listing from org B."""
    from app.repositories.loyalty_campaigns_repo import db_create_campaign, db_list_campaigns
    from app.services.tenant_context import tenant_scope

    org_a, org_b = org_ids

    # Create in org A
    await _set_scope(db_conn, org_a)
    with tenant_scope(org_a):
        camp = await db_create_campaign(
            org_id=org_a, name="OrgA Private Camp", trigger_type="manual", message_template="secret"
        )

    # List from org B — must be empty
    await _set_scope(db_conn, org_b)
    with tenant_scope(org_b):
        result = await db_list_campaigns()

    ids = [c["id"] for c in result]
    assert camp["id"] not in ids


# ── No-scope guard test ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_scope_raises(db_conn, org_ids):
    """Calling campaigns repo without tenant_scope raises TenantNotSetError."""
    from app.repositories.loyalty_campaigns_repo import db_list_campaigns
    from app.services.tenant_context import TenantNotSetError

    # Ensure no scope is active (clear any previously set GUC)
    await db_conn.execute("SELECT set_config('app.org_id', '', true)")

    with pytest.raises(TenantNotSetError):
        await db_list_campaigns()
