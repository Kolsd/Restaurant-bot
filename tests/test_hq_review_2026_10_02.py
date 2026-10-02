"""
Mesio HQ walk-through 2026-10-02 — every section opened with the test key.

What it found, and what these tests hold in place:
  - Platform counts (home, Monitoring, Ctrl+K, Superadmin) read RLS tables
    over a bare mesio_app pool connection, which sees zero rows, and counted
    web orders only: table rounds — most of the volume — never showed.
  - The onboarding tab read a column that no longer exists (carta never
    "done") and WhatsApp-era stages; now it is the owner's own checklist.
  - Saving a CRM follow-up date was a 500 (a str sent to a timestamp).
  - Every new sede was flagged "no orders in 7 days" on its first day.
  - The hard delete guard counted orders RLS hid, so it never refused.
"""
from __future__ import annotations

import uuid

import pytest

from app.main import app
from app.routes.deps import verify_superadmin
from tests.test_walkthrough_2026_10_01 import (  # noqa: F401 — fixtures
    TEST_DB_URL, _owner, _q, _run, client, made,
)

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")


@pytest.fixture
def as_superadmin():
    app.dependency_overrides[verify_superadmin] = lambda: None
    yield
    app.dependency_overrides.pop(verify_superadmin, None)


def _round(org_id, location_id, status="entregado", total=20000):
    oid = f"r-{uuid.uuid4().hex[:10]}"
    _run(_q(
        "INSERT INTO table_orders (id, table_id, table_name, phone, items, status, total, base_order_id, sub_number, "
        "station, branch_id, org_id, location_id) "
        "VALUES ($1, 't-x', 'Mesa 1', 'web:x', '[]'::jsonb, $2, $3, $1, 1, 'all', $4::int, $5, $4::bigint)",
        oid, status, total, location_id, org_id, fetch="none", org_id=org_id,
    ))


def _web_order(org_id, location_id, status="entregado", total=30000):
    _run(_q(
        "INSERT INTO orders (id, phone, items, order_type, subtotal, total, org_id, location_id, status) "
        "VALUES ($1, '+573000000000', '[]'::jsonb, 'recoger', $2, $2, $3, $4, $5)",
        f"o-{uuid.uuid4().hex[:10]}", total, org_id, location_id, status, fetch="none", org_id=org_id,
    ))


def test_platform_counts_include_table_rounds_and_skip_cancelled(client, made, as_superadmin):
    before = client.get("/api/internal/analytics/overview").json()
    me = _owner(client, made)
    org_id, loc = me["org_id"], me["location_id"]
    _round(org_id, loc)
    _round(org_id, loc)
    _round(org_id, loc, status="cancelled")
    _web_order(org_id, loc)
    _web_order(org_id, loc, status="rechazado")

    after = client.get("/api/internal/analytics/overview").json()
    assert after["orders"]["today"] - before["orders"]["today"] == 3
    assert after["orders"]["sales_today"] - before["orders"]["sales_today"] == 70000
    assert after["restaurants"]["active_today"] - before["restaurants"]["active_today"] == 1

    detail = client.get(f"/api/internal/admin/restaurant/{org_id}").json()["stats"]
    assert detail["orders_30d"] == 3
    assert detail["table_orders_30d"] == 2
    assert detail["revenue_30d"] == 70000

    name = _run(_q("SELECT name FROM organizations WHERE id = $1", org_id, fetch="val"))
    hits = client.get("/api/internal/search", params={"q": name}).json()["results"]
    tenant = next(h for h in hits if h["type"] == "tenant" and h["id"] == org_id)
    assert "sin actividad" not in tenant["subtitle"]


def test_hard_delete_refuses_a_restaurant_that_sells_at_its_tables(client, made, as_superadmin):
    me = _owner(client, made)
    _round(me["org_id"], me["location_id"])
    r = client.delete(f"/api/internal/admin/organizations/{me['org_id']}", params={"hard": "true"})
    assert r.status_code == 409
    r = client.delete(f"/api/internal/admin/locations/{me['location_id']}", params={"hard": "true"})
    assert r.status_code == 409
    still = _run(_q("SELECT id FROM organizations WHERE id = $1", me["org_id"], fetch="val"))
    assert still == me["org_id"]


def test_onboarding_tab_is_the_owners_checklist(client, made, as_superadmin):
    me = _owner(client, made)
    data = client.get(f"/api/internal/admin/organizations/{me['org_id']}/onboarding").json()["data"]
    stages = {s["key"]: s for s in data["stages"]}
    assert set(stages) == {"created", "menu", "tables", "team", "first_order"}
    assert stages["created"]["done"] and not stages["first_order"]["done"]
    assert stages["team"]["optional"]

    _round(me["org_id"], me["location_id"])
    again = client.get(f"/api/internal/admin/organizations/{me['org_id']}/onboarding").json()["data"]
    assert {s["key"]: s for s in again["stages"]}["first_order"]["done"]
    assert again["score"] > data["score"]


def test_a_new_sede_is_not_a_churn_risk_on_day_one(client, made, as_superadmin):
    me = _owner(client, made)
    codes = lambda: {f["code"] for f in client.get(f"/api/internal/hq/orgs/{me['org_id']}").json()["flags"]}  # noqa: E731
    assert "no_activity_7d" not in codes()
    _run(_q("UPDATE locations SET created_at = NOW() - INTERVAL '8 days' WHERE id = $1",
            me["location_id"], fetch="none"))
    assert "no_activity_7d" in codes()


def test_crm_follow_up_dates_save_and_clear(client, as_superadmin):
    created = client.post("/api/internal/crm/prospects", json={
        "restaurant_name": f"Pizzería {uuid.uuid4().hex[:6]}", "phone": "3000000000",
        "email": "Dueno@Ejemplo.com", "next_follow_up": "2026-10-05T10:00",
    })
    assert created.status_code == 200
    p = created.json()["prospect"]
    try:
        assert p["email"] == "dueno@ejemplo.com"
        assert p["next_follow_up"].startswith("2026-10-05T10:00")

        moved = client.patch(f"/api/internal/crm/prospects/{p['id']}", json={"next_follow_up": "2026-10-07T15:30"})
        assert moved.status_code == 200
        assert moved.json()["prospect"]["next_follow_up"].startswith("2026-10-07T15:30")

        cleared = client.patch(f"/api/internal/crm/prospects/{p['id']}", json={"next_follow_up": None, "city": "Cali"})
        assert cleared.status_code == 200
        assert cleared.json()["prospect"]["next_follow_up"] is None
    finally:
        client.delete(f"/api/internal/crm/prospects/{p['id']}")


def test_superadmin_creates_an_owner_without_a_password(client, made, as_superadmin, monkeypatch):
    # PM decision 2026-10-02: Mesio never sets or sees a password. The new
    # owner gets a code by email and creates their own.
    from unittest.mock import AsyncMock
    from app.services.password_hash import verify_password

    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.provisioning.send_account_setup", sent)
    me = _owner(client, made)
    email = f"socio.{uuid.uuid4().hex[:8]}@ejemplo.com"
    made[1].append(email)

    assert client.post("/api/internal/admin/create-user",
                       json={"restaurant_id": me["org_id"], "username": "sin-arroba"}).status_code == 400
    r = client.post("/api/internal/admin/create-user",
                    json={"restaurant_id": me["org_id"], "username": email.upper(), "password": "IgnoredPass1"})
    assert r.status_code == 200, r.text
    assert r.json()["setup_email_sent"] is True
    assert sent.await_args.args[0] == email
    pw_hash = _run(_q("SELECT password_hash FROM users WHERE username = $1", email, fetch="val"))
    assert pw_hash and not verify_password("IgnoredPass1", pw_hash)


def test_account_setup_email_carries_a_code_never_a_password():
    from app.services.email_templates import render_account_setup_email
    from app.services.provisioning import setup_url

    url = setup_url("dueno@ejemplo.com")
    assert url.endswith("/reset-password#codigo=dueno%40ejemplo.com")
    subject, html, text = render_account_setup_email("El Rancho", "dueno@ejemplo.com", "482913", url)
    assert "482913" in html and "482913" in text and url in text
    assert "temporal" not in text.lower()
