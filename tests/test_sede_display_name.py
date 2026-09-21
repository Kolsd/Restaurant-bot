"""tests/test_sede_display_name.py

A diner has to be able to tell WHICH sede they are dealing with.

PM 2026-09-20: each sede is its own restaurant. The `restaurants` view named
every one of them after the organization (`COALESCE(o.name, l.name)`), so a
chain's Sede Norte and Sede Centro introduced themselves identically — in the
chat greeting, on the order status page, in the confirmation email and on the
owner's dashboard.

`display_name` (migration 0092) is "Marca · Sede" when the org runs more than
one, and the plain brand when it runs one. The SQL and the Python mirror in
app/services/naming.py must agree, so both are exercised here.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.services.naming import restaurant_display_name

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")


# -- The Python mirror (no DB needed) --------------------------------------

def test_single_sede_shows_the_plain_brand():
    """Almost every customer today. A suffix here would be pure noise."""
    assert restaurant_display_name("Burger Palace", "Principal", 1) == "Burger Palace"


def test_several_sedes_name_the_sede():
    assert restaurant_display_name("Burger Palace", "Sede Norte", 2) == "Burger Palace · Sede Norte"


def test_a_sede_named_after_the_org_is_not_repeated():
    """Seeds and older onboarding name the first location after the org."""
    assert restaurant_display_name("Burger Palace", "burger palace", 3) == "Burger Palace"


def test_a_blank_sede_name_disambiguates_nothing():
    assert restaurant_display_name("Burger Palace", "   ", 2) == "Burger Palace"
    assert restaurant_display_name("Burger Palace", None, 2) == "Burger Palace"


def test_an_org_with_no_name_falls_back_to_the_sede():
    assert restaurant_display_name(None, "Sede Norte", 2) == "Sede Norte"


# -- The SQL, which is the source of truth ---------------------------------

pytestmark_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed(sede_names: list[str]) -> dict:
    suffix = uuid.uuid4().hex[:10]
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            f"Marca {suffix}", f"dn-{suffix}",
        )
        loc_ids = []
        for name in sede_names:
            loc_ids.append(await conn.fetchval(
                "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
                org_id, name,
            ))
        return {"org_id": org_id, "loc_ids": loc_ids, "org_name": f"Marca {suffix}"}
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


async def _display_names(loc_ids: list[int]) -> list[str]:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        rows = await conn.fetch(
            "SELECT id, display_name FROM restaurants WHERE id = ANY($1::bigint[]) ORDER BY id",
            loc_ids,
        )
        return [r["display_name"] for r in rows]
    finally:
        await conn.close()


@pytestmark_db
def test_view_leaves_a_single_sede_alone():
    info = _run(_seed(["Principal"]))
    try:
        assert _run(_display_names(info["loc_ids"])) == [info["org_name"]]
    finally:
        _run(_teardown(info["org_id"]))


@pytestmark_db
def test_view_names_each_sede_when_there_are_several():
    info = _run(_seed(["Sede Norte", "Sede Centro"]))
    try:
        names = _run(_display_names(info["loc_ids"]))
        assert names == [
            f"{info['org_name']} · Sede Norte",
            f"{info['org_name']} · Sede Centro",
        ]
    finally:
        _run(_teardown(info["org_id"]))


@pytestmark_db
def test_view_and_python_mirror_agree():
    """app/services/naming.py exists because some paths merge an org row with
    a location row instead of reading the view. The two must not drift."""
    info = _run(_seed(["Sede Norte", "Sede Sur"]))
    try:
        from_sql = _run(_display_names(info["loc_ids"]))
        from_python = [
            restaurant_display_name(info["org_name"], sede, 2)
            for sede in ("Sede Norte", "Sede Sur")
        ]
        assert from_sql == from_python
    finally:
        _run(_teardown(info["org_id"]))


@pytestmark_db
def test_view_does_not_repeat_a_sede_named_after_the_org():
    info = _run(_seed(["Sede Norte"]))
    try:
        conn_org_name = info["org_name"]
        # Add a second sede carrying the org's own name — what the demo seed
        # and older onboarding produce.
        async def _add():
            conn = await asyncpg.connect(TEST_DB_URL)
            try:
                return await conn.fetchval(
                    "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
                    info["org_id"], conn_org_name,
                )
            finally:
                await conn.close()

        twin = _run(_add())
        names = _run(_display_names([twin]))
        assert names == [conn_org_name], names
    finally:
        _run(_teardown(info["org_id"]))
