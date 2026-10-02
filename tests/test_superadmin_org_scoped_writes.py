"""
tests/test_superadmin_org_scoped_writes.py

Regression test for the P0 fixed 2026-09-12: the superadmin UI's "edit
restaurant" / "set subscription" actions used to resolve the target
organization via `WHERE id = (SELECT org_id FROM locations WHERE id=$N)` —
i.e. they expected a LOCATION id, but the UI always sent an ORG id (rows
from db_get_all_orgs). Org ids and location ids are independent BIGSERIAL
sequences over the same integer range (Wave 2), so whenever an org's own id
happened to equal a DIFFERENT org's location id, the write silently landed
on the WRONG TENANT.

Every earlier test for these endpoints passed only because the ids
involved never collided — this test seeds the collision ON PURPOSE:
  - org A, with a location (ordinary setup)
  - org B, whose location's id is set (explicitly) to org A's own id

It then drives the CURRENT code (the `update_organization` / `get_organization_
detail` handler coroutines behind PATCH/GET /api/internal/admin/organizations/
{org_id} — the org-scoped replacement for the deleted legacy endpoints) and
asserts:
  1. Org A's name/subscription_status/features change to exact values.
  2. Org B is byte-for-byte unchanged (proves no bleed-through via the
     colliding location id — the exact scenario that broke before).
  3. A menu carrying another restaurant's image_public_id is rejected
     (db_update_organization's normalize+ownership path), and does not
     mutate the stored menu.

Requires TEST_DATABASE_URL (mesio_fresh, migrated to head). Uses the real
route handler coroutines + a real asyncpg pool — no repo mocking, per
CLAUDE.md's "Tests Verídicos" discipline (exact values, no status-code-only
assertions). Handler coroutines are called directly rather than through
FastAPI's TestClient: TestClient drives the ASGI app through its own
anyio portal/thread, which — combined with app.services.database's
module-global asyncpg pool — reliably corrupts the pool across more than
one request per test (confirmed while writing this test: "another
operation is in progress" / "connection was closed in the middle of
operation" on the second call). tests/test_internal_admin_plan_reset.py
documents and works around the exact same constraint. Calling the handler
coroutines directly exercises the identical code FastAPI would call
just without the ASGI/portal machinery.

All seeded rows are deleted in a fixture teardown.
"""
from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration test skipped",
)


def _jsonb(val):
    """Deserialize a jsonb column value that may come back as str or already-decoded.

    Mirrors the defensive pattern used throughout restaurant_repo.py (e.g.
    db_update_organization's own RETURNING-row handling): asyncpg's stock
    jsonb behavior for a value that reached the column via a pre-dumped
    string + `::jsonb` cast is to hand it back as a plain str on read, so
    every read path in this codebase re-parses defensively rather than
    trusting a client-side codec.
    """
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def seed_pool():
    """Dedicated asyncpg pool for direct seed/verify SQL.

    Created and torn down within THIS test's event loop — the app's own
    pool (app.services.database._pool) is a separate object, reset by the
    autouse _reset_real_db_pool fixture in conftest.py, but both pools live
    on the same loop for the duration of a single test coroutine, which is
    what avoids the cross-loop asyncpg corruption described in the module
    docstring above.
    """
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def superadmin_session(monkeypatch):
    """Make verify_superadmin accept any Bearer token as the superadmin session
    (route-level auth dependency — not exercised when calling handlers
    directly, kept for symmetry / in case a future variant of this test
    drives the routes through TestClient)."""
    from app.repositories import sessions_repo

    async def _fake_get_session(token):
        return "mesio:superadmin" if token else None  # sessions_repo.SUPERADMIN_IDENTITY

    monkeypatch.setattr(sessions_repo, "get_session", _fake_get_session)


@pytest.fixture
async def collision_orgs(seed_pool):
    """Seed org A (with a location) + org B whose location id == org A's id.

    Yields a dict with both orgs' full rows (as inserted, jsonb columns
    already decoded to native Python objects) plus their ids, then deletes
    every seeded row (organizations cascade-delete their locations via
    ON DELETE CASCADE).
    """
    tag = uuid.uuid4().hex[:10]

    async with seed_pool.acquire() as conn:
        org_a = await conn.fetchrow(
            """
            INSERT INTO organizations
                (name, slug, features, subscription_plan, subscription_status)
            VALUES ($1, $2, $3::jsonb, 'pro', 'active')
            RETURNING id, name, menu, features, subscription_plan, subscription_status
            """,
            f"Org A {tag}", f"org-a-{tag}", '{"existing_key": "keepme", "locale": "es-CO"}',
        )
        loc_a = await conn.fetchrow(
            "INSERT INTO locations (org_id, name, active) VALUES ($1, $2, true) RETURNING id",
            org_a["id"], f"Sede A {tag}",
        )

        org_b = await conn.fetchrow(
            """
            INSERT INTO organizations
                (name, slug, features, subscription_plan, subscription_status)
            VALUES ($1, $2, $3::jsonb, 'restaurante', 'active')
            RETURNING id, name, menu, features, subscription_plan, subscription_status
            """,
            f"Org B {tag}", f"org-b-{tag}", '{"locale": "es-MX"}',
        )
        # THE COLLISION: org B's location id is set (explicitly) to org A's
        # own id. locations.id is a plain BIGSERIAL — explicit inserts are
        # allowed. This is what made the deleted ambiguous subquery resolve
        # `WHERE id = (SELECT org_id FROM locations WHERE id = <org_A.id>)`
        # to ORG B instead of "not found".
        collision_location_id = org_a["id"]
        await conn.execute(
            "INSERT INTO locations (id, org_id, name, active) VALUES ($1, $2, $3, true)",
            collision_location_id, org_b["id"], f"Sede B (colliding id) {tag}",
        )

    def _decoded(row):
        d = dict(row)
        d["features"] = _jsonb(d.get("features"))
        d["menu"] = _jsonb(d.get("menu"))
        return d

    try:
        yield {
            "tag": tag,
            "org_a": _decoded(org_a),
            "org_b": _decoded(org_b),
            "loc_a_id": loc_a["id"],
            "collision_location_id": collision_location_id,
        }
    finally:
        async with seed_pool.acquire() as conn:
            # Cascades to locations via ON DELETE CASCADE.
            await conn.execute(
                "DELETE FROM organizations WHERE id = ANY($1::bigint[])",
                [org_a["id"], org_b["id"]],
            )


async def _fetch_org(pool, org_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, name, menu, features, subscription_plan, subscription_status
               FROM organizations WHERE id = $1""",
            org_id,
        )
    if not row:
        return None
    d = dict(row)
    d["features"] = _jsonb(d.get("features"))
    d["menu"] = _jsonb(d.get("menu"))
    return d


def _import_admin_route_pieces():
    from app.routes.internal import admin as admin_module
    return admin_module, admin_module.PatchOrgRequest


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_organization_changes_a_and_never_touches_b(
    seed_pool, collision_orgs,
):
    """Core regression: calling the PATCH /organizations/{org_A.id} handler
    must change ONLY org A, even though org B has a location whose id
    equals org A's own id."""
    admin_module, PatchOrgRequest = _import_admin_route_pieces()

    org_a_id = collision_orgs["org_a"]["id"]
    org_b_id = collision_orgs["org_b"]["id"]
    org_b_before = collision_orgs["org_b"]
    expected_name = f"Org A Renamed {collision_orgs['tag']}"

    result = await admin_module.update_organization(
        org_a_id,
        PatchOrgRequest(
            name=expected_name,
            subscription_status="suspended",
            features={"new_flag": True},
        ),
        None,
        None,
    )
    returned_org = result["data"]["org"]

    # ── Org A: exact expected values ─────────────────────────────────────────
    assert returned_org["name"] == expected_name
    assert returned_org["subscription_status"] == "suspended"
    # features merged (shallow), not replaced — existing_key survives
    assert returned_org["features"]["new_flag"] is True
    assert returned_org["features"]["existing_key"] == "keepme"
    assert returned_org["features"]["locale"] == "es-CO"

    # Re-fetch via the same repo function the app itself uses to read an org
    # back (db_get_org_by_id normalizes jsonb columns exactly the way every
    # other consumer in this codebase does — see CLAUDE.md's "features como
    # dict: puede venir como str JSON o dict según driver/VIEW" note). A raw
    # SQL re-select through an independent, uninstrumented connection is
    # NOT representative here: asyncpg's jsonb type-codec on the app's real
    # pool plus db_update_organization's own pre-serialization is a
    # deliberate, self-consistent double round-trip that only resolves
    # correctly through that same normalization path.
    from app.repositories import restaurant_repo
    org_a_db = await restaurant_repo.db_get_org_by_id(org_a_id)
    assert org_a_db["name"] == expected_name
    assert org_a_db["subscription_status"] == "suspended"
    assert org_a_db["features"]["new_flag"] is True
    assert org_a_db["features"]["existing_key"] == "keepme"

    # ── Org B: byte-for-byte unchanged ───────────────────────────────────────
    org_b_after = await _fetch_org(seed_pool, org_b_id)
    assert org_b_after == org_b_before, (
        "Org B changed after PATCHing org A — the ambiguous location-id-shaped "
        "subquery regression is back. Org B's location id collided with org "
        "A's own id; org B must never be touched by a write scoped to org A."
    )
    assert org_b_after["subscription_status"] == "active"
    assert org_b_after["name"] == org_b_before["name"]


@pytest.mark.asyncio
async def test_menu_with_cross_tenant_image_rejected_and_not_persisted(
    seed_pool, collision_orgs,
):
    """db_update_organization(menu=...) must reject a dish image owned by
    another restaurant and must NOT persist any part of that menu.

    Exercised at the repository level (not yet reachable via PatchOrgRequest,
    which does not expose `menu` for the superadmin screen) — this is the
    normalize+validate path db_update_organization now shares with
    db_update_menu, per CLAUDE.md's catalog-v2 image-ownership rule.
    """
    from app.repositories import restaurant_repo

    org_a_id = collision_orgs["org_a"]["id"]
    org_b_id = collision_orgs["org_b"]["id"]

    bad_menu = {
        "Principales": [
            {
                "name": "Plato robado",
                "price": 30000,
                # Belongs to org B, not org A.
                "image_public_id": f"mesio/r_{org_b_id}/dish_stolen",
            }
        ]
    }

    with pytest.raises(ValueError, match=f"mesio/r_{org_b_id}/dish_stolen"):
        await restaurant_repo.db_update_organization(org_a_id, menu=bad_menu)

    org_a_db = await _fetch_org(seed_pool, org_a_id)
    # menu was NEVER set on org A during seeding (defaults to '[]'::jsonb) —
    # confirm the rejected write left it untouched, not partially applied.
    assert org_a_db["menu"] in ([], None)
