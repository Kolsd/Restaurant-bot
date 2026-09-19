"""
tests/test_staff_location_edit.py
====================================
Chunk 8 of the delivery/pickup web wave (docs/claude/delivery-web.md), part B
— THE STAFF SEDE PICKER:

  - PUT /api/staff/{id} now accepts location_id (StaffUpdate), validated with
    the SAME ownership check _resolve_new_staff_location already applies on
    create (app/routes/staff.py);
  - a foreign sede is refused (403) and the staff row is left untouched;
  - GET /api/staff flags unassigned staff via the `multi_sede` envelope flag
    + each staff row's own `location_id` (null == "Sin sede" in the UI);
  - GET /api/staff/locations lists the org's sedes for the picker.

Real database, real HTTP (TestClient) — mirrors tests/test_delivery_cashier.py
and tests/test_waiter_alerts_location.py's seed/auth conventions in this repo.
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


def _get(client, url, **kwargs):
    _reset_pool()
    return client.get(url, **kwargs)


def _put(client, url, **kwargs):
    _reset_pool()
    return client.put(url, **kwargs)


_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _auth_as_owner(monkeypatch, org_id: int, username: str = "staff_edit_owner"):
    from app.services import database as db

    async def _verify_token(token):
        return username

    async def _get_user(uname):
        if uname != username:
            return None
        return {
            "username": username, "branch_id": None,
            "org_id": org_id, "location_id": None,
            "role": "owner", "restaurant_name": "",
        }

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db, "db_get_user", _get_user)


async def _seed_org(name: str) -> int:
    """features.staff_tips=true — every /api/staff/* route this file exercises
    sits behind require_module('staff_tips') (see app/routes/staff.py's
    _MODULE_DEPS)."""
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:8]
        return await conn.fetchval(
            "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
            name, f"{name.lower().replace(' ', '-')}-{suffix}", '{"staff_tips": true}',
        )
    finally:
        await conn.close()


async def _seed_location(org_id: int, name: str = "Sede") -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            "INSERT INTO locations (org_id, name, active) VALUES ($1, $2, true) RETURNING id",
            org_id, name,
        )
    finally:
        await conn.close()


async def _seed_staff(org_id: int, location_id: int | None, role: str = "mesero") -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            """INSERT INTO staff (name, role, username, org_id, location_id, active)
               VALUES ($1, $2, $3, $4, $5, true) RETURNING id::text""",
            f"Staff {uuid.uuid4().hex[:6]}", role, f"staff_{uuid.uuid4().hex[:10]}",
            org_id, location_id,
        )
    finally:
        await conn.close()


async def _fetch_staff_location(staff_id: str) -> int | None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval("SELECT location_id FROM staff WHERE id = $1::uuid", staff_id)
    finally:
        await conn.close()


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM staff WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


# ── PUT /api/staff/{id}: location_id persists + is validated ──────────────


def test_update_staff_with_own_org_sede_persists(client, monkeypatch):
    org_id = _run(_seed_org("Staff Edit Org"))
    loc_a = _run(_seed_location(org_id, "Sede A"))
    loc_b = _run(_seed_location(org_id, "Sede B"))
    staff_id = _run(_seed_staff(org_id, loc_a))
    try:
        _auth_as_owner(monkeypatch, org_id)
        resp = _put(
            client, f"/api/staff/{staff_id}",
            headers=_AUTH_HEADERS, json={"location_id": loc_b},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["staff"]["location_id"] == loc_b

        stored = _run(_fetch_staff_location(staff_id))
        assert stored == loc_b
    finally:
        _run(_teardown_org(org_id))


def test_update_staff_with_foreign_sede_is_refused_and_row_unchanged(client, monkeypatch):
    org_a = _run(_seed_org("Staff Edit Org A"))
    org_b = _run(_seed_org("Staff Edit Org B"))
    loc_a = _run(_seed_location(org_a, "Sede A"))
    loc_foreign = _run(_seed_location(org_b, "Sede Foreign"))
    staff_id = _run(_seed_staff(org_a, loc_a))
    try:
        _auth_as_owner(monkeypatch, org_a, username="staff_edit_owner_a")
        resp = _put(
            client, f"/api/staff/{staff_id}",
            headers=_AUTH_HEADERS, json={"location_id": loc_foreign},
        )
        assert resp.status_code == 403, resp.text

        stored = _run(_fetch_staff_location(staff_id))
        assert stored == loc_a, "a refused update must not touch the row"
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))


def test_update_staff_other_fields_still_work_without_location(client, monkeypatch):
    """location_id is optional on StaffUpdate — omitting it must not force a
    sede change or break the existing update path."""
    org_id = _run(_seed_org("Staff Edit Org Plain"))
    loc_a = _run(_seed_location(org_id, "Sede A"))
    staff_id = _run(_seed_staff(org_id, loc_a))
    try:
        _auth_as_owner(monkeypatch, org_id, username="staff_edit_owner_plain")
        resp = _put(
            client, f"/api/staff/{staff_id}",
            headers=_AUTH_HEADERS, json={"phone": "3009998877"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["staff"]["phone"] == "3009998877"

        stored = _run(_fetch_staff_location(staff_id))
        assert stored == loc_a, "location must stay untouched when not included in the patch"
    finally:
        _run(_teardown_org(org_id))


# ── GET /api/staff: multi_sede flag + unassigned staff are visible ─────────


def test_list_staff_flags_multi_sede_and_shows_unassigned(client, monkeypatch):
    org_id = _run(_seed_org("Staff List Org"))
    loc_a = _run(_seed_location(org_id, "Sede A"))
    _run(_seed_location(org_id, "Sede B"))  # second sede -> multi_sede True
    assigned_id = _run(_seed_staff(org_id, loc_a))
    unassigned_id = _run(_seed_staff(org_id, None))
    try:
        _auth_as_owner(monkeypatch, org_id, username="staff_list_owner")
        resp = _get(client, "/api/staff", headers=_AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["multi_sede"] is True

        by_id = {s["id"]: s for s in data["staff"]}
        assert by_id[assigned_id]["location_id"] == loc_a
        assert by_id[unassigned_id]["location_id"] is None
    finally:
        _run(_teardown_org(org_id))


def test_list_staff_single_sede_org_is_not_flagged_multi(client, monkeypatch):
    org_id = _run(_seed_org("Staff List Single Sede Org"))
    loc_only = _run(_seed_location(org_id, "Única Sede"))
    _run(_seed_staff(org_id, loc_only))
    try:
        _auth_as_owner(monkeypatch, org_id, username="staff_list_owner_single")
        resp = _get(client, "/api/staff", headers=_AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["multi_sede"] is False
    finally:
        _run(_teardown_org(org_id))


# ── GET /api/staff/locations: the sede picker's data source ────────────────


def test_list_staff_locations_returns_org_sedes_only(client, monkeypatch):
    org_a = _run(_seed_org("Staff Locations Org A"))
    org_b = _run(_seed_org("Staff Locations Org B"))
    loc_a1 = _run(_seed_location(org_a, "A Sede 1"))
    loc_a2 = _run(_seed_location(org_a, "A Sede 2"))
    _run(_seed_location(org_b, "B Sede"))
    try:
        _auth_as_owner(monkeypatch, org_a, username="staff_locations_owner")
        resp = _get(client, "/api/staff/locations", headers=_AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        ids = {loc["id"] for loc in resp.json()["locations"]}
        assert ids == {loc_a1, loc_a2}
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))
