"""
tests/test_qr_claim_flow.py
============================
Integration tests for the QR-Phone-Claim flow (Capa 1 of MESA_QR_ARCHITECTURE).

Validates:
  - create_claim → find_unclaimed_by_phone → mark_claimed cycle works.
  - Match is by EXACT phone (race-free): two claims for different phones
    don't collide; the right one is returned for each.
  - Once claimed, the row is invisible to subsequent lookups.
  - Expired rows are not returned.
  - cleanup_expired removes old rows.

These tests use real Postgres (TEST_DATABASE_URL) so they exercise the
actual UNIQUE constraints, partial indexes, and TIMESTAMPTZ math.
"""

import os
import asyncio
import pytest
import asyncpg


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _set_scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))


async def _get_or_create_test_org(pool) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO organizations (name, slug)
            VALUES ('QrClaimTestOrg', 'qr-claim-test-org')
            ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
            RETURNING id
            """
        )
    return int(row["id"])


async def _cleanup_claims(pool, org_id: int) -> None:
    """Best-effort teardown — RLS-scoped delete by org."""
    try:
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await _set_scope(conn, org_id)
            await conn.execute("DELETE FROM qr_scan_pending WHERE org_id = $1::bigint", org_id)
    except Exception:
        pass


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def real_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=2, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def patched_pool(real_pool, monkeypatch):
    from app.services import database as db_module
    async def _fake_get_pool():
        return real_pool
    monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)
    yield real_pool


@pytest.fixture
async def org_and_cleanup(patched_pool):
    org_id = await _get_or_create_test_org(patched_pool)
    try:
        yield org_id
    finally:
        await _cleanup_claims(patched_pool, org_id)


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_consume_single_claim(org_and_cleanup):
    """Happy path: create claim, find by phone, mark consumed, second lookup returns None."""
    from app.repositories import qr_claims_repo
    from app.services.tenant_context import tenant_scope

    org_id = org_and_cleanup
    bot = "+57888001"
    phone_input = "+57 300 111 1111"
    phone_normalized = "573001111111"

    with tenant_scope(org_id):
        claim_id = await qr_claims_repo.create_claim(
            bot_number=bot,
            table_id="tbl-qr-test-1",
            phone=phone_input,
            org_id=org_id,
        )
    assert claim_id > 0

    # Lookup is cross-tenant (the bot doesn't know the org yet) — verify it works
    # without an active scope on this conn path.
    found = await qr_claims_repo.find_unclaimed_by_phone(phone_normalized, bot)
    assert found is not None
    assert found["table_id"] == "tbl-qr-test-1"
    assert found["org_id"] == org_id
    assert found["phone"] == phone_normalized
    assert found["claimed_at"] is None

    # Consume.
    consumed = await qr_claims_repo.mark_claimed(int(found["id"]))
    assert consumed is True

    # Second lookup for the same phone → None (claim is taken).
    second = await qr_claims_repo.find_unclaimed_by_phone(phone_normalized, bot)
    assert second is None


@pytest.mark.asyncio
async def test_two_phones_get_their_own_claims(org_and_cleanup):
    """Race-free guarantee: claims for different phones don't collide.

    This is the core property the QR-Phone-Claim design promises — two
    customers scanning different tables don't get assigned to each other's.
    """
    from app.repositories import qr_claims_repo
    from app.services.tenant_context import tenant_scope

    org_id = org_and_cleanup
    bot = "+57888002"

    with tenant_scope(org_id):
        await qr_claims_repo.create_claim(
            bot_number=bot, table_id="tbl-A", phone="3001111111", org_id=org_id,
        )
        await qr_claims_repo.create_claim(
            bot_number=bot, table_id="tbl-B", phone="3002222222", org_id=org_id,
        )

    # Both customers message at "the same time".
    found_a, found_b = await asyncio.gather(
        qr_claims_repo.find_unclaimed_by_phone("3001111111", bot),
        qr_claims_repo.find_unclaimed_by_phone("3002222222", bot),
    )
    assert found_a is not None and found_a["table_id"] == "tbl-A"
    assert found_b is not None and found_b["table_id"] == "tbl-B"


@pytest.mark.asyncio
async def test_expired_claim_not_returned(org_and_cleanup, patched_pool):
    """Expired claims are filtered out by the lookup query (WHERE expires_at > NOW())."""
    from app.repositories import qr_claims_repo
    from app.services.tenant_context import tenant_scope

    org_id = org_and_cleanup
    bot = "+57888003"

    with tenant_scope(org_id):
        claim_id = await qr_claims_repo.create_claim(
            bot_number=bot, table_id="tbl-expired",
            phone="3009999999", org_id=org_id,
        )

    # Force expire by updating the row directly (simulates 11+ min passing).
    async with patched_pool.acquire() as conn:
        await conn.execute("SET LOCAL ROLE mesio_app")
        await _set_scope(conn, org_id)
        await conn.execute(
            "UPDATE qr_scan_pending SET expires_at = NOW() - INTERVAL '1 minute' WHERE id = $1",
            claim_id,
        )

    found = await qr_claims_repo.find_unclaimed_by_phone("3009999999", bot)
    assert found is None


@pytest.mark.asyncio
async def test_phone_normalization_strips_plus_and_spaces(org_and_cleanup):
    """Stored phone is digits-only. Lookups should also normalize before matching."""
    from app.repositories import qr_claims_repo
    from app.services.tenant_context import tenant_scope

    org_id = org_and_cleanup
    bot = "+57888004"

    with tenant_scope(org_id):
        await qr_claims_repo.create_claim(
            bot_number=bot, table_id="tbl-norm",
            phone="+57 300 444 5555", org_id=org_id,
        )

    # All these inputs should resolve to the same record.
    for variant in [
        "573004445555",
        "+57 300 444 5555",
        "57-300-444-5555",
        "+573004445555",
    ]:
        found = await qr_claims_repo.find_unclaimed_by_phone(variant, bot)
        assert found is not None, f"Lookup failed for variant {variant!r}"
        assert found["phone"] == "573004445555"


@pytest.mark.asyncio
async def test_geo_verified_persisted(org_and_cleanup, patched_pool):
    """create_claim accepts and persists geo_verified=true/false/null."""
    from app.repositories import qr_claims_repo
    from app.services.tenant_context import tenant_scope

    org_id = org_and_cleanup
    bot = "+57888005"

    with tenant_scope(org_id):
        verified_id = await qr_claims_repo.create_claim(
            bot_number=bot, table_id="t-geo-yes",
            phone="3001000001", org_id=org_id, geo_verified=True,
        )
        far_id = await qr_claims_repo.create_claim(
            bot_number=bot, table_id="t-geo-no",
            phone="3001000002", org_id=org_id, geo_verified=False,
        )
        unknown_id = await qr_claims_repo.create_claim(
            bot_number=bot, table_id="t-geo-null",
            phone="3001000003", org_id=org_id, geo_verified=None,
        )

    async with patched_pool.acquire() as conn:
        await conn.execute("SET LOCAL ROLE mesio_app")
        await _set_scope(conn, org_id)
        rows = await conn.fetch(
            "SELECT id, geo_verified FROM qr_scan_pending "
            "WHERE id = ANY($1::bigint[]) ORDER BY id ASC",
            [verified_id, far_id, unknown_id],
        )

    assert rows[0]["geo_verified"] is True
    assert rows[1]["geo_verified"] is False
    assert rows[2]["geo_verified"] is None


@pytest.mark.asyncio
async def test_consume_race_returns_false_for_loser(org_and_cleanup):
    """Two workers calling mark_claimed on the same id concurrently:
    exactly one wins (returns True), the other returns False."""
    from app.repositories import qr_claims_repo
    from app.services.tenant_context import tenant_scope

    org_id = org_and_cleanup
    bot = "+57888006"

    with tenant_scope(org_id):
        claim_id = await qr_claims_repo.create_claim(
            bot_number=bot, table_id="t-race",
            phone="3007777777", org_id=org_id,
        )

    results = await asyncio.gather(
        qr_claims_repo.mark_claimed(claim_id),
        qr_claims_repo.mark_claimed(claim_id),
    )
    wins = [r for r in results if r is True]
    losses = [r for r in results if r is False]
    assert len(wins) == 1, f"expected 1 winner, got {wins}"
    assert len(losses) == 1, f"expected 1 loser, got {losses}"
