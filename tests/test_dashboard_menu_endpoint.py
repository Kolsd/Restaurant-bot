"""
tests/test_dashboard_menu_endpoint.py
=====================================
GET /api/dashboard/menu — the carta the full editor loads.

It used to resolve the restaurant by its WhatsApp number
(`db_get_menu(bot_number)`). Since self-serve signup deliberately does not
claim `organizations.whatsapp_number` — the column is UNIQUE, and claiming
it stopped an owner from registering a second restaurant — every org
created that way had no phone, so this endpoint returned `{}` and the
editor opened blank on top of a carta that was sitting in the database.

It also contradicted the standing rule that nothing reads a carta through
`db_get_menu(bot_number)` any more.

Requires TEST_DATABASE_URL: the bug is about how a row is looked up, so a
mocked repository would not have caught it and would not catch it again.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import app

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)

_MENU = {
    "Entradas": [{"name": "Empanadas", "price": 9000, "description": "3 unidades"}],
    "Fuertes": [{"name": "Bandeja paisa", "price": 32000, "description": ""}],
}


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("DISABLE_EMBEDDED_WORKER", "1")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def org_without_phone():
    """A committed org with a carta and NO whatsapp_number, cleaned up after."""
    import asyncio

    slug = f"carta-test-{uuid.uuid4().hex[:8]}"

    async def _create():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            org_id = await conn.fetchval(
                """INSERT INTO organizations (name, slug, whatsapp_number, menu)
                   VALUES ($1, $2, NULL, $3::jsonb) RETURNING id""",
                f"Carta Test {slug}", slug, json.dumps(_MENU),
            )
            return org_id
        finally:
            await conn.close()

    async def _drop(org_id: int):
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))
            await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
        finally:
            await conn.close()

    org_id = asyncio.run(_create())
    yield org_id
    asyncio.run(_drop(org_id))


def _as_owner_of(org_id: int):
    """Only auth is mocked; the lookup under test runs for real."""
    return patch.multiple(
        "app.routes.settings_routes",
        require_auth=AsyncMock(return_value=None),
        get_current_restaurant=AsyncMock(return_value={
            "id": org_id, "name": "Carta Test", "whatsapp_number": None,
        }),
    )


def test_an_org_with_no_whatsapp_number_still_gets_its_carta(client, org_without_phone):
    with _as_owner_of(org_without_phone):
        r = client.get("/api/dashboard/menu")

    assert r.status_code == 200, r.text
    menu = r.json()["menu"]
    assert sorted(menu) == ["Entradas", "Fuertes"], (
        "the editor opened blank on top of a saved carta"
    )
    assert menu["Fuertes"][0]["name"] == "Bandeja paisa"
    assert menu["Fuertes"][0]["price"] == 32000


def test_an_org_with_no_carta_yet_gets_an_empty_one_not_a_500(client, org_without_phone):
    """A brand-new restaurant has no dishes; that is not an error."""
    import asyncio

    async def _clear():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            # `menu` is NOT NULL with a default of {} — which is exactly the
            # shape a brand-new org is born with.
            await conn.execute(
                "UPDATE organizations SET menu = '{}'::jsonb WHERE id = $1",
                org_without_phone,
            )
        finally:
            await conn.close()

    asyncio.run(_clear())

    with _as_owner_of(org_without_phone):
        r = client.get("/api/dashboard/menu")

    assert r.status_code == 200, r.text
    assert r.json()["menu"] == {}
