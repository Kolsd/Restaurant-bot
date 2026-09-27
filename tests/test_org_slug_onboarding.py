"""tests/test_org_slug_onboarding.py

Every organization must end up with a public slug.

`/pedir/{slug}` is the ONE public link a restaurant hands out for delivery
and pickup (docs/claude/delivery-web.md), resolved by
delivery_repo.db_get_org_by_slug. `organizations.slug` is nullable, and the
only writer that ever set it was migration 0034's backfill from the legacy
`restaurants` table — CRM conversion (app/routes/internal/crm.py) called
db_create_organization without a slug, and no route anywhere updates the
column. So every customer onboarded after 0034 had slug = NULL and an
ordering link that 404s: the whole web channel, unreachable, for exactly
the customers we are onboarding now.

Real DB, not mocks: the uniqueness these tests are about is a DB constraint.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


async def _drop_orgs(org_ids: list[int]) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM locations WHERE org_id = ANY($1::bigint[])", org_ids)
        await conn.execute("DELETE FROM organizations WHERE id = ANY($1::bigint[])", org_ids)
    finally:
        await conn.close()


def _create_org(name: str, **kwargs) -> dict:
    from app.repositories import restaurant_repo
    _reset_pool()
    return _run(restaurant_repo.db_create_organization(name=name, **kwargs))


def test_new_org_gets_a_slug_without_the_caller_passing_one():
    """CRM conversion passes no slug — the org must still be reachable."""
    created = []
    try:
        org = _create_org("El Fogón de Ana")
        created.append(org["id"])

        assert org["slug"], "an org with no slug has no /pedir link at all"
        assert org["slug"] == "el-fog-n-de-ana" or org["slug"].startswith("el-fog"), org["slug"]

        # And it actually resolves through the public entry point's own lookup.
        from app.repositories import delivery_repo
        _reset_pool()
        found = _run(delivery_repo.db_get_org_by_slug(org["slug"]))
        assert found is not None and found["id"] == org["id"]
    finally:
        _run(_drop_orgs(created))


def test_two_orgs_with_the_same_name_get_different_slugs():
    """Two unrelated restaurants sharing a name is normal, not an error —
    the second must not blow up on the UNIQUE constraint nor steal the first
    one's link."""
    created = []
    try:
        name = f"Donde Pepe {uuid.uuid4().hex[:6]}"
        first = _create_org(name)
        created.append(first["id"])
        second = _create_org(name)
        created.append(second["id"])

        assert first["slug"] and second["slug"]
        assert first["slug"] != second["slug"]
        assert second["slug"].startswith(first["slug"])
    finally:
        _run(_drop_orgs(created))


def test_an_explicit_slug_is_still_honoured():
    """The internal admin route lets the founder choose one — generation must
    only fill the gap, never override."""
    created = []
    try:
        chosen = f"mi-slug-{uuid.uuid4().hex[:8]}"
        org = _create_org(
            "Nombre Cualquiera",
            slug=chosen,
        )
        created.append(org["id"])
        assert org["slug"] == chosen
    finally:
        _run(_drop_orgs(created))


def test_no_existing_organization_is_left_without_a_slug():
    """Migration 0088 backfills the rows that already exist. Asserted against
    the live schema, so a future writer that reintroduces a NULL slug fails
    here instead of silently shipping an unreachable ordering link."""
    async def _count_null() -> int:
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM organizations WHERE slug IS NULL OR trim(slug) = ''"
            )
        finally:
            await conn.close()

    assert _run(_count_null()) == 0
