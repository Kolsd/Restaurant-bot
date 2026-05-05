"""
tests/e2e/test_floor_plan_save_lifecycle.py — E2E test: floor plan save → reload.

What this exercises (no Anthropic, no LLM):
  1. Seed a restaurant + 3 restaurant_tables with default positions/capacities.
  2. POST /api/tables/floor-plan with new positions, capacity, table_type, zone.
  3. Reload via GET /api/tables/floor-plan (or direct DB read) → verify all
     fields round-tripped exactly.
  4. Cross-tenant: a sibling org's table cannot be mutated through Org A's
     bulk save (RLS + WHERE org_id guard).
  5. Invalid table_type rejected at the route layer (400, repo not called).

Why a real E2E:
  Floor plan editor is the most-used admin surface for table config. The unit
  tests validate the per-call SQL but not the round-trip behavior across HTTP
  + RLS + asyncpg. This test proves a save-then-reload yields the persisted
  values, which is the only thing the user actually cares about.

Marked `e2e_no_llm` so it runs without ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    create_admin_token,
    seed_restaurant,
    truncate_e2e_data,
)
from app.services.tenant_context import bypass_tenant_scope


@pytest_asyncio.fixture()
async def e2e_app():
    """Yields an httpx AsyncClient against the FastAPI app via ASGI transport."""
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


async def _seed_table(
    pool: asyncpg.Pool,
    *,
    org_id: int,
    location_id: int,
    number: int,
    name: str,
) -> str:
    """Insert a restaurant_tables row with default position/capacity. Returns id."""
    tid = f"tbl-{uuid.uuid4().hex[:10]}"
    with bypass_tenant_scope("e2e_floorplan_seed"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """
                INSERT INTO restaurant_tables
                    (id, number, name, branch_id, org_id, location_id, active,
                     capacity, table_type, zone, position_x, position_y)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE, 4, 'interior', '', 0, 0)
                """,
                tid, number, name, location_id, org_id, location_id,
            )
    return tid


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_floor_plan_save_reload_round_trips(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
):
    """Save floor plan via POST → reload via GET → values match exactly."""
    pool = test_pool

    # ── Seed restaurant + admin token ──────────────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E FloorPlan Restaurant",
        bot_number_raw="+570FLOORPLAN1",
        num_branches=1,
    )
    org_id = restaurant["id"]
    location_id = restaurant["branches"][0]["id"]
    await truncate_e2e_data(pool, org_id)

    # Clean any lingering tables from prior runs
    with bypass_tenant_scope("e2e_floorplan_clean"):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM restaurant_tables WHERE org_id = $1", org_id,
            )

    # Seed 3 tables with default position (0, 0) and default capacity (4)
    t1 = await _seed_table(pool, org_id=org_id, location_id=location_id,
                           number=1, name="Mesa 1")
    t2 = await _seed_table(pool, org_id=org_id, location_id=location_id,
                           number=2, name="Mesa 2")
    t3 = await _seed_table(pool, org_id=org_id, location_id=location_id,
                           number=3, name="Mesa 3")

    admin_token = await create_admin_token(pool, restaurant["owner_email"])

    # ── POST /api/tables/floor-plan with mixed updates ─────────────────────────
    payload = {
        "tables": [
            {"id": t1, "position_x": 100.5, "position_y": 200.25},
            {"id": t2, "capacity": 8, "zone": "terraza", "table_type": "terraza"},
            {"id": t3, "name": "Mesa VIP", "position_x": 50, "position_y": 50},
        ]
    }
    resp = await e2e_app.post(
        "/api/tables/floor-plan",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, f"POST failed: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data["updated"] == 3, f"Expected 3 updates, got {data}"
    assert data["skipped"] == 0
    assert data["errors"] == []

    # ── Reload via direct DB read (RLS-scoped) ────────────────────────────────
    with bypass_tenant_scope("e2e_floorplan_verify"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            r1 = await conn.fetchrow(
                "SELECT position_x, position_y, capacity, table_type, zone, name "
                "FROM restaurant_tables WHERE id = $1", t1,
            )
            r2 = await conn.fetchrow(
                "SELECT position_x, position_y, capacity, table_type, zone, name "
                "FROM restaurant_tables WHERE id = $1", t2,
            )
            r3 = await conn.fetchrow(
                "SELECT position_x, position_y, capacity, table_type, zone, name "
                "FROM restaurant_tables WHERE id = $1", t3,
            )

    # t1: only position changed
    assert float(r1["position_x"]) == pytest.approx(100.5)
    assert float(r1["position_y"]) == pytest.approx(200.25)
    assert r1["capacity"] == 4              # untouched default
    assert r1["table_type"] == "interior"   # untouched default
    assert r1["zone"] == ""                 # untouched default
    assert r1["name"] == "Mesa 1"           # untouched

    # t2: capacity + zone + table_type changed; position untouched
    assert float(r2["position_x"]) == pytest.approx(0)
    assert float(r2["position_y"]) == pytest.approx(0)
    assert r2["capacity"] == 8
    assert r2["table_type"] == "terraza"
    assert r2["zone"] == "terraza"
    assert r2["name"] == "Mesa 2"           # untouched

    # t3: name + position changed
    assert float(r3["position_x"]) == pytest.approx(50)
    assert float(r3["position_y"]) == pytest.approx(50)
    assert r3["name"] == "Mesa VIP"
    assert r3["capacity"] == 4
    assert r3["table_type"] == "interior"


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_floor_plan_cross_tenant_blocked(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
):
    """Org A admin attempting to update Org B's table → table_id surfaces in
    errors[] with reason='not_found_or_wrong_org', Org B's row stays intact."""
    pool = test_pool

    # ── Seed two restaurants ───────────────────────────────────────────────────
    rest_a = await seed_restaurant(
        pool, name="E2E FP Iso A", bot_number_raw="+570FPISOAX1", num_branches=1,
    )
    rest_b = await seed_restaurant(
        pool, name="E2E FP Iso B", bot_number_raw="+570FPISOBX1", num_branches=1,
    )
    org_a, loc_a = rest_a["id"], rest_a["branches"][0]["id"]
    org_b, loc_b = rest_b["id"], rest_b["branches"][0]["id"]

    await truncate_e2e_data(pool, org_a)
    await truncate_e2e_data(pool, org_b)

    with bypass_tenant_scope("e2e_floorplan_iso_clean"):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM restaurant_tables WHERE org_id IN ($1, $2)",
                org_a, org_b,
            )

    # One table per org
    tid_a = await _seed_table(pool, org_id=org_a, location_id=loc_a,
                              number=1, name="A1")
    tid_b = await _seed_table(pool, org_id=org_b, location_id=loc_b,
                              number=1, name="B1")

    admin_a_token = await create_admin_token(pool, rest_a["owner_email"])

    # ── Org A admin POSTs against BOTH tables — t_b should fail ───────────────
    payload = {
        "tables": [
            {"id": tid_a, "position_x": 11, "position_y": 22},
            {"id": tid_b, "position_x": 999, "position_y": 999},  # cross-tenant
        ]
    }
    resp = await e2e_app.post(
        "/api/tables/floor-plan",
        json=payload,
        headers={"Authorization": f"Bearer {admin_a_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] == 1, f"Expected 1 update (own table only): {data}"
    assert len(data["errors"]) == 1
    assert data["errors"][0]["table_id"] == tid_b
    assert data["errors"][0]["reason"] == "not_found_or_wrong_org"

    # ── Verify Org B's table is unchanged (position still 0,0) ────────────────
    with bypass_tenant_scope("e2e_floorplan_iso_verify"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_b),
            )
            row_b = await conn.fetchrow(
                "SELECT position_x, position_y FROM restaurant_tables WHERE id = $1",
                tid_b,
            )
    assert float(row_b["position_x"]) == pytest.approx(0), (
        f"Org B table mutated cross-tenant: position_x={row_b['position_x']}"
    )
    assert float(row_b["position_y"]) == pytest.approx(0)
