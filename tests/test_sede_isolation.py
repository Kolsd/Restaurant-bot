"""tests/test_sede_isolation.py

An employee of one sede must never see another sede's data (PM 2026-09-20).

Every staff-facing listing used to resolve its sede from the client-supplied
`X-Branch-ID` / `X-Location-ID` header, and almost none of them checked the
caller's role first. Two separate holes, both reachable by a waiter or a cook
with nothing but curl:

  - naming ANOTHER sede in the header and being served it, and
  - naming NO sede and being served EVERY sede of the org, which is what the
    staff app actually did by default.

The fix is one resolver, `app/routes/deps.py::resolve_sede_filter`: owner and
admin may span sedes, everyone else — `gerente` included — is pinned to their
own `location_id`. These tests drive the HTTP endpoints against a real
database, because what is being asserted is which ROWS come back.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)

_AUTH = {"Authorization": "Bearer test-token"}


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _run(coro):
    _reset_pool()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get(client, url, **kwargs):
    _reset_pool()
    return client.get(url, **kwargs)


# -- Seeding ---------------------------------------------------------------

async def _seed_two_sede_org() -> dict:
    """One org, two sedes, each with its own table order, waiter alert and
    table."""
    suffix = uuid.uuid4().hex[:10]
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, menu, features, plan_code) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb, 'pro') RETURNING id",
            f"Sede Iso {suffix}", f"sede-iso-{suffix}", "{}", "{}",
        )
        # Both sedes share the org's tenant key: only the sede filter can
        # separate them.
        loc_a = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1,$2) RETURNING id",
            org_id, "Sede A",
        )
        loc_b = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1,$2) RETURNING id",
            org_id, "Sede B",
        )

        for table_number, (loc, label) in enumerate(((loc_a, "A"), (loc_b, "B")), start=1):
            # location_id and branch_id are the same sede but different column
            # types (bigint vs integer), so they get their own placeholders —
            # asyncpg cannot deduce one type for a parameter used as both.
            await conn.execute(
                "INSERT INTO table_orders (id, org_id, location_id, branch_id, table_id, "
                "table_name, phone, status, station, items, total) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,'recibido','kitchen',$8::jsonb,0)",
                f"to-{label}-{suffix}", org_id, loc, loc,
                f"table-{label}-{suffix}", f"Mesa {label}", f"web:{uuid.uuid4().hex}",
                json.dumps([{"name": "Plato", "quantity": 1}]),
            )
            await conn.execute(
                "INSERT INTO waiter_alerts (org_id, location_id, table_id, "
                "table_name, phone, alert_type, message) "
                "VALUES ($1,$2,$3,$4,$5,'cuenta','Pide la cuenta')",
                org_id, loc, f"table-{label}-{suffix}",
                f"Mesa {label}", f"web:{uuid.uuid4().hex}",
            )
            await conn.execute(
                "INSERT INTO restaurant_tables (id, org_id, location_id, branch_id, number, name) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                f"table-{label}-{suffix}", org_id, loc, loc, table_number, f"Mesa {label}",
            )

        # One dashboard user per sede, so /api/team/users has something to
        # leak. Without these the endpoint returns [] and the test passes for
        # the wrong reason.
        for loc, label in ((loc_a, "A"), (loc_b, "B")):
            await conn.execute(
                "INSERT INTO users (username, password_hash, restaurant_name, role, "
                "branch_id, org_id, location_id) "
                "VALUES ($1,'x',$2,'mesero',$3,$4,$5)",
                f"user-{label}-{suffix}", f"Sede {label}", org_id, org_id, loc,
            )

        return {
            "org_id": org_id, "loc_a": loc_a, "loc_b": loc_b,
            "suffix": suffix,
        }
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        for table in ("waiter_alerts", "table_orders", "restaurant_tables", "staff", "users"):
            await conn.execute(f"DELETE FROM {table} WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def two_sedes():
    info = _run(_seed_two_sede_org())
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


def _auth_as(monkeypatch, *, org_id: int, location_id: int | None, role: str,
             username: str = "sede_user"):
    """Resolve auth to a user of this org with this role and sede, without
    mocking the restaurant lookups themselves — the real org/location
    resolution in deps.py is what these tests are about."""
    from app.services import database as db

    async def _verify_token(token):
        return username

    async def _get_user(uname):
        if uname != username:
            return None
        return {
            "username": username, "branch_id": org_id, "org_id": org_id,
            "location_id": location_id, "role": role, "restaurant_name": "",
        }

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db, "db_get_user", _get_user)


# -- The header can no longer move a non-admin to another sede -------------

def test_waiter_header_cannot_switch_sede(client, two_sedes, monkeypatch):
    """A mesero of sede A asking for sede B gets sede A anyway."""
    _auth_as(monkeypatch, org_id=two_sedes["org_id"],
             location_id=two_sedes["loc_a"], role="mesero")

    resp = _get(client, "/api/table-orders",
                headers={**_AUTH, "X-Branch-ID": str(two_sedes["loc_b"])})
    assert resp.status_code == 200, resp.text
    names = {o["table_name"] for o in resp.json()["orders"]}
    assert names == {"Mesa A"}, names


def test_waiter_without_a_header_sees_only_their_sede(client, two_sedes, monkeypatch):
    """The real default in the staff app: no header at all. This used to
    return every sede of the org."""
    _auth_as(monkeypatch, org_id=two_sedes["org_id"],
             location_id=two_sedes["loc_a"], role="mesero")

    resp = _get(client, "/api/table-orders", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    names = {o["table_name"] for o in resp.json()["orders"]}
    assert names == {"Mesa A"}, names


def test_owner_still_sees_every_sede_and_can_pick_one(client, two_sedes, monkeypatch):
    """The fix must not cost the owner their cross-sede view."""
    _auth_as(monkeypatch, org_id=two_sedes["org_id"], location_id=None,
             role="owner", username="owner_user")

    all_sedes = _get(client, "/api/table-orders", headers=_AUTH)
    assert all_sedes.status_code == 200, all_sedes.text
    assert {o["table_name"] for o in all_sedes.json()["orders"]} == {"Mesa A", "Mesa B"}

    just_b = _get(client, "/api/table-orders",
                  headers={**_AUTH, "X-Branch-ID": str(two_sedes["loc_b"])})
    assert just_b.status_code == 200, just_b.text
    assert {o["table_name"] for o in just_b.json()["orders"]} == {"Mesa B"}


def test_gerente_is_pinned_to_their_own_sede(client, two_sedes, monkeypatch):
    """A gerente runs ONE sede — same rule as any other employee, even
    though staff_sections.ADMIN_ROLES counts them as an admin role for
    deciding which SECTIONS they see."""
    _auth_as(monkeypatch, org_id=two_sedes["org_id"],
             location_id=two_sedes["loc_b"], role="gerente", username="gerente_user")

    resp = _get(client, "/api/table-orders",
                headers={**_AUTH, "X-Branch-ID": str(two_sedes["loc_a"])})
    assert resp.status_code == 200, resp.text
    assert {o["table_name"] for o in resp.json()["orders"]} == {"Mesa B"}


def test_staff_without_a_sede_is_refused_not_shown_everything(client, two_sedes, monkeypatch):
    """The dangerous default: no location_id must mean 403, never "all"."""
    _auth_as(monkeypatch, org_id=two_sedes["org_id"], location_id=None,
             role="mesero", username="homeless_waiter")

    resp = _get(client, "/api/table-orders", headers=_AUTH)
    assert resp.status_code == 403, resp.text


# -- The same rule on the other staff-facing listings ----------------------

def test_waiter_alerts_are_scoped_to_the_callers_sede(client, two_sedes, monkeypatch):
    _auth_as(monkeypatch, org_id=two_sedes["org_id"],
             location_id=two_sedes["loc_a"], role="mesero")

    resp = _get(client, "/api/waiter-alerts",
                headers={**_AUTH, "X-Branch-ID": str(two_sedes["loc_b"])})
    assert resp.status_code == 200, resp.text
    tables = {a.get("table_name") for a in resp.json()["alerts"]}
    assert tables == {"Mesa A"}, tables


def test_floor_plan_is_scoped_to_the_callers_sede(client, two_sedes, monkeypatch):
    _auth_as(monkeypatch, org_id=two_sedes["org_id"],
             location_id=two_sedes["loc_a"], role="mesero")

    resp = _get(client, "/api/tables/floor-plan", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    tables = payload.get("tables") if isinstance(payload, dict) else payload
    names = {t.get("name") for t in (tables or [])}
    assert "Mesa B" not in names, names


def test_team_users_are_scoped_to_the_callers_sede(client, two_sedes, monkeypatch):
    """Any authenticated account used to be able to list every employee of
    every sede simply by omitting the filter."""
    _auth_as(monkeypatch, org_id=two_sedes["org_id"],
             location_id=two_sedes["loc_a"], role="mesero")

    resp = _get(client, "/api/team/users", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    users = resp.json()["users"]
    assert users, "seed must give this endpoint something to return"
    locs = {u.get("location_id") for u in users}
    assert locs == {two_sedes["loc_a"]}, locs
