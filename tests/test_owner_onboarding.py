"""
tests/test_owner_onboarding.py
==============================
The setup checklist a restaurant sees for itself (GET /api/onboarding).

The pure half pins the rules the checklist is built on: only verifiable
steps, optional steps never block 100%, a malformed carta counts as empty
rather than as done, and trial days round up. The DB half pins that the
counts are scoped by org and — for tables — by sede, and that nothing is
resolved through a WhatsApp number an org created by self-serve signup does
not have.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.onboarding import build_steps, count_dishes, trial_days_left

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


# ── Pure rules ────────────────────────────────────────────────────────────────

def _steps(**counts):
    base = {"dish_count": 0, "table_count": 0, "staff_count": 0, "order_count": 0}
    base.update(counts)
    return {s.key: s for s in build_steps(**base)}


def test_a_brand_new_restaurant_has_every_step_pending():
    steps = _steps()
    assert [k for k, s in steps.items() if s.done] == []
    assert set(steps) == {"menu", "tables", "team", "first_order"}


def test_every_step_says_where_to_go():
    """A checklist that names what is missing without a way to fix it is a nag."""
    for step in _steps().values():
        assert step.actions, f"{step.key} has no action"
        assert all(a.href.startswith("/") for a in step.actions)


def test_the_qr_print_action_appears_once_there_are_tables_to_print():
    """There is nothing to print before a table exists."""
    no_tables = _steps()["tables"]
    assert not any("qr-sheet" in a.href for a in no_tables.actions)

    with_tables = _steps(table_count=4)["tables"]
    printer = [a for a in with_tables.actions if "qr-sheet" in a.href]
    assert printer and printer[0].external, "the print sheet opens in its own tab"


def test_printing_is_not_a_step_that_ticks_itself_off():
    """Nothing can know that paper came out of a printer."""
    assert "qr" not in _steps(table_count=4)


def test_the_team_step_is_optional():
    """A one-person dark kitchen must be able to finish the checklist."""
    assert _steps()["team"].optional is True
    assert all(not s.optional for k, s in _steps().items() if k != "team")


@pytest.mark.parametrize("junk", [None, [], "texto", {"Entradas": "no es lista"},
                                  {"Entradas": [{"precio": 1000}, "x", {"name": "  "}]}])
def test_a_malformed_carta_counts_as_empty_not_as_done(junk):
    assert count_dishes(junk) == 0


def test_dishes_are_counted_across_categories():
    menu = {"Entradas": [{"name": "Empanadas"}], "Fuertes": [{"name": "Bandeja"}, {"name": "Ajiaco"}]}
    assert count_dishes(menu) == 3


def test_trial_days_round_up_and_say_zero_when_over():
    now = datetime.now(tz=timezone.utc)
    assert trial_days_left(None) is None
    assert trial_days_left(now + timedelta(hours=20)) == 1, "20 hours left is still a day"
    assert trial_days_left(now + timedelta(days=13, hours=1)) == 14
    assert trial_days_left(now - timedelta(minutes=1)) == 0
    # Naive timestamps from the DB are UTC by project convention.
    assert trial_days_left((now + timedelta(days=2, hours=1)).replace(tzinfo=None)) == 3


# ── The service, end to end over mocked reads ─────────────────────────────────

def test_optional_steps_do_not_hold_back_completion():
    from app.services import onboarding

    with patch("app.repositories.sede_menu_repo.db_get_org_menu",
               new=AsyncMock(return_value={"Fuertes": [{"name": "Bandeja"}]})), \
         patch("app.repositories.onboarding_repo.db_onboarding_counts",
               new=AsyncMock(return_value={"tables": 3, "staff": 0, "orders": 1})), \
         patch("app.repositories.restaurant_repo.db_get_org_by_id",
               new=AsyncMock(return_value={"id": 1, "comp_until": None})):
        result = asyncio.run(onboarding.get_checklist(1, 5))

    assert result["done"] == result["total"] == 3
    assert result["complete"] is True, "no team invited, and that is fine"


def test_an_unreadable_count_shows_as_not_done_rather_than_failing():
    """Telling someone to re-check a step beats telling them they are done."""
    from app.services import onboarding

    with patch("app.repositories.sede_menu_repo.db_get_org_menu",
               new=AsyncMock(side_effect=RuntimeError("db down"))), \
         patch("app.repositories.onboarding_repo.db_onboarding_counts",
               new=AsyncMock(side_effect=RuntimeError("db down"))), \
         patch("app.repositories.restaurant_repo.db_get_org_by_id",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        result = asyncio.run(onboarding.get_checklist(1, 5))

    assert result["done"] == 0
    assert result["complete"] is False
    assert result["trial_days_left"] is None


# ── The counts, against Postgres ──────────────────────────────────────────────

_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")


async def _seed_two_orgs():
    """Org A with two sedes (2 tables in one, 1 in the other), org B with 5 tables.

    Ids are allocated by the database, so org and location ids of the two
    tenants interleave — a count that forgot its org filter would pick up
    B's rows.
    """
    suffix = uuid.uuid4().hex[:8]
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        org_a = await conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            f"Onb A {suffix}", f"onb-a-{suffix}")
        org_b = await conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            f"Onb B {suffix}", f"onb-b-{suffix}")
        loc_a1 = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1, 'A1') RETURNING id", org_a)
        loc_a2 = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1, 'A2') RETURNING id", org_a)
        loc_b = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1, 'B1') RETURNING id", org_b)

        async def _table(org, loc, n):
            await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org))
            await conn.execute(
                # branch_id is INTEGER and location_id BIGINT, so the same value
                # goes in as two parameters — one $n cannot carry both types.
                """INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active)
                   VALUES ($1, $2, $3, $4, $5, $6, TRUE)""",
                f"t-{suffix}-{org}-{loc}-{n}", n, f"Mesa {n}", loc, loc, org)

        try:
            for n in (1, 2):
                await _table(org_a, loc_a1, n)
            await _table(org_a, loc_a2, 1)
            for n in range(1, 6):
                await _table(org_b, loc_b, n)
        except Exception:
            # These rows are committed; a half-built seed must not outlive
            # the test that failed to build it.
            await conn.close()
            await _drop(org_a, org_b)
            raise
        return org_a, org_b, loc_a1, loc_a2
    finally:
        if not conn.is_closed():
            await conn.close()


async def _drop(*orgs):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        for org in orgs:
            await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org))
            await conn.execute("DELETE FROM restaurant_tables WHERE org_id = $1", org)
            await conn.execute("DELETE FROM locations WHERE org_id = $1", org)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("DISABLE_EMBEDDED_WORKER", "1")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def two_orgs():
    ids = asyncio.run(_seed_two_orgs())
    yield ids
    asyncio.run(_drop(ids[0], ids[1]))


def _checklist_for(client, org_id: int, location_id: int | None):
    """Only auth is mocked; the counts run against Postgres through the app pool."""
    with patch.multiple(
        "app.routes.onboarding_routes",
        require_auth=AsyncMock(return_value=None),
        get_current_user=AsyncMock(return_value={"username": "o", "role": "owner", "org_id": org_id}),
        get_current_restaurant=AsyncMock(return_value={"id": org_id, "whatsapp_number": None}),
        resolve_sede_filter=lambda request, user: location_id,
    ):
        r = client.get("/api/onboarding")
    assert r.status_code == 200, r.text
    return {s["key"]: s for s in r.json()["steps"]}


@_db
def test_table_counts_are_per_sede_and_never_cross_tenants(client, two_orgs):
    org_a, org_b, loc_a1, loc_a2 = two_orgs

    sede_1 = _checklist_for(client, org_a, loc_a1)["tables"]
    assert sede_1["done"] is True
    assert sede_1["detail"].startswith("2 mesa"), sede_1["detail"]

    sede_2 = _checklist_for(client, org_a, loc_a2)["tables"]
    assert sede_2["detail"].startswith("1 mesa"), sede_2["detail"]

    # Owner view with no sede picked: the whole org, still not B's five.
    whole_org = _checklist_for(client, org_a, None)["tables"]
    assert whole_org["detail"].startswith("3 mesa"), whole_org["detail"]


@_db
def test_an_org_with_no_whatsapp_number_gets_a_real_checklist(client, two_orgs):
    """Every count is keyed by org_id; none goes through a phone number."""
    org_a, _, loc_a1, _ = two_orgs
    steps = _checklist_for(client, org_a, loc_a1)
    assert steps["tables"]["done"] is True
    assert steps["menu"]["done"] is False
    assert steps["first_order"]["done"] is False
