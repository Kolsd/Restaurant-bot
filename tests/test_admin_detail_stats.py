"""Regression test for db_get_restaurant_detail_stats (superadmin detail panel).

The table-orders count used

    AND (SELECT whatsapp_number FROM restaurants
         WHERE id = table_orders.branch_id OR id = $1 LIMIT 1) = $1

binding the same $1 both as an id (bigint) and against whatsapp_number (text),
so every call raised ``operator does not exist: text = bigint`` and
GET /api/internal/admin/restaurant/{id} returned 500. It also mixed location ids
with org ids — the collision class removed in 5abfa8a — and counted users
through the ambiguous ``users.branch_id`` superseded by ``users.org_id`` (0081).

These tests seed two organizations and assert exact per-org counts, so they
catch both the crash and any cross-org leakage.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration test skipped",
)


@pytest.fixture
async def seed_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_org(conn, tag: str, suffix: str) -> dict:
    org = await conn.fetchrow(
        "INSERT INTO organizations (name, slug, whatsapp_number) "
        "VALUES ($1, $2, $3) RETURNING id, whatsapp_number",
        f"Stats Org {suffix} {tag}", f"stats-{suffix}-{tag}", f"stats:{suffix}:{tag}",
    )
    loc_id = await conn.fetchval(
        "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
        org["id"], f"Sede {suffix} {tag}",
    )
    return {"org_id": org["id"], "wa": org["whatsapp_number"], "location_id": loc_id}


async def _seed_table_order(conn, org: dict, *, status: str, age: str) -> None:
    await conn.execute(
        "INSERT INTO table_orders "
        "(id, table_id, table_name, phone, org_id, branch_id, status, total, created_at) "
        f"VALUES ($1, 't-stats', 'Mesa', 'web:stats', $2, $3, $4, 10000, NOW() - INTERVAL '{age}')",
        f"to-{uuid.uuid4().hex}", org["org_id"], org["location_id"], status,
    )


@pytest.fixture
async def two_orgs(seed_pool):
    tag = uuid.uuid4().hex[:8]
    usernames: list[str] = []
    async with seed_pool.acquire() as conn:
        a = await _seed_org(conn, tag, "a")
        b = await _seed_org(conn, tag, "b")

        # Org A: 2 counted, 1 cancelled (excluded), 1 older than 30 days (excluded)
        await _seed_table_order(conn, a, status="recibido", age="1 day")
        await _seed_table_order(conn, a, status="entregado", age="2 days")
        await _seed_table_order(conn, a, status="cancelado", age="1 day")
        await _seed_table_order(conn, a, status="entregado", age="40 days")
        # Org B: exactly 1 counted
        await _seed_table_order(conn, b, status="recibido", age="1 day")

        for org, n in ((a, 2), (b, 1)):
            for i in range(n):
                u = f"stats_{tag}_{org['org_id']}_{i}"
                usernames.append(u)
                await conn.execute(
                    "INSERT INTO users (username, password_hash, restaurant_name, org_id) "
                    "VALUES ($1, 'x', 'Stats', $2)",
                    u, org["org_id"],
                )
    try:
        yield a, b
    finally:
        async with seed_pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE username = ANY($1::text[])", usernames)
            # table_orders and locations cascade from organizations
            await conn.execute(
                "DELETE FROM organizations WHERE id = ANY($1::bigint[])",
                [a["org_id"], b["org_id"]],
            )


async def test_detail_stats_does_not_crash_and_counts_exactly(two_orgs):
    from app.repositories.restaurant_repo import db_get_restaurant_detail_stats

    a, _b = two_orgs
    stats = await db_get_restaurant_detail_stats(a["org_id"], a["wa"])

    assert stats["table_orders_30d"] == 2  # cancelled and >30d rows excluded
    assert stats["users"] == 2


async def test_detail_stats_never_counts_another_org(two_orgs):
    from app.repositories.restaurant_repo import db_get_restaurant_detail_stats

    a, b = two_orgs
    stats_b = await db_get_restaurant_detail_stats(b["org_id"], b["wa"])

    assert stats_b["table_orders_30d"] == 1
    assert stats_b["users"] == 1

    # And org A's numbers are unaffected by org B's rows
    stats_a = await db_get_restaurant_detail_stats(a["org_id"], a["wa"])
    assert stats_a["table_orders_30d"] == 2
    assert stats_a["users"] == 2
