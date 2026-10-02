"""
Mesio HQ ficha — GET /api/internal/hq/orgs/{org_id}.

Real signup, real rows in a real DB: each sede's numbers are its own (ids
collide across tenants on purpose), the health flags fire on the situations
the runbook describes, kitchen time comes from ready_at, and only Mesio's
superadmin can read any of it.
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


def _sede(org_id: int, name: str) -> int:
    return _run(_q("INSERT INTO locations (org_id, name, address, active) VALUES ($1, $2, 'Calle 1', TRUE) RETURNING id",
                   org_id, name, fetch="val"))


def _round(org_id, location_id, minutes_ago, status, total, ready_after_min=None):
    oid = f"r-{uuid.uuid4().hex[:10]}"
    _run(_q(
        "INSERT INTO table_orders (id, table_id, table_name, phone, items, status, total, base_order_id, sub_number, "
        "station, branch_id, org_id, location_id, created_at, ready_at) "
        "VALUES ($1, 't-x', 'Mesa 1', 'web:x', '[]'::jsonb, $2, $3, $1, 1, 'all', $4::int, $5, $4::bigint, "
        "NOW() - make_interval(mins => $6), "
        "CASE WHEN $7::int IS NULL THEN NULL ELSE NOW() - make_interval(mins => $6) + make_interval(mins => $7::int) END)",
        oid, status, total, location_id, org_id, minutes_ago, ready_after_min, fetch="none", org_id=org_id,
    ))
    return oid


def test_the_ficha_keeps_each_sede_apart_and_flags_what_needs_help(client, made, as_superadmin):
    me = _owner(client, made)
    org_id, principal = me["org_id"], me["location_id"]
    norte = _sede(org_id, "Norte")

    # Principal: two served rounds (kitchen 10 and 20 min) and one stuck for an hour.
    for mins, ready in ((30, 10), (40, 20), (50, 10), (55, 20), (58, 10)):
        _round(org_id, principal, mins, "entregado", 20000, ready)
    _round(org_id, principal, 60, "recibido", 15000)
    # Norte: a pickup order still open after two hours.
    _run(_q(
        "INSERT INTO orders (id, phone, items, order_type, subtotal, total, org_id, location_id, status, created_at) "
        "VALUES ($1, '+573000000000', '[]'::jsonb, 'recoger', 30000, 30000, $2, $3, 'en_preparacion', NOW() - INTERVAL '2 hours')",
        f"o-{uuid.uuid4().hex[:10]}", org_id, norte, fetch="none", org_id=org_id,
    ))
    # Another customer's rows on a sede whose id we don't own must never count.
    other = _owner(client, made)
    _round(other["org_id"], other["location_id"], 5, "recibido", 999000)

    snap = client.get(f"/api/internal/hq/orgs/{org_id}").json()
    sedes = {s["name"]: s for s in snap["sedes"]}
    p, n = sedes[next(k for k in sedes if sedes[k]["id"] == principal)], sedes["Norte"]

    assert p["operation"]["table_rounds_30d"] == 6
    assert p["operation"]["sales_30d"] == 115000
    assert p["operation"]["kitchen_p50_min"] == 10.0
    assert p["operation"]["kitchen_samples_7d"] == 5
    assert n["operation"]["table_rounds_30d"] == 0
    assert n["operation"]["pickup_30d"] == 1

    p_codes = {f["code"] for f in p["flags"]}
    n_codes = {f["code"] for f in n["flags"]}
    assert "stuck_rounds" in p_codes and "stuck_rounds" not in n_codes
    assert "stuck_orders" in n_codes and "stuck_orders" not in p_codes
    assert "no_tables" in n_codes
    stuck = next(f for f in snap["flags"] if f["code"] == "stuck_rounds")
    assert stuck["sede_id"] == principal and stuck["count"] == 1
    assert stuck["where"] and stuck["fix"], "every flag says where to look and how to fix it"

    assert snap["business"]["billing_status"] == "trial"
    assert snap["business"]["mrr_cop"] == 0  # a trial bills nothing
    assert snap["business"]["active_sedes"] == 2
    assert snap["org"]["order_link"].startswith("/pedir/")
    assert any("owner" in u["role"] for u in snap["people"]["users"])


def test_a_paused_or_suspended_org_is_the_first_thing_the_ficha_says(client, made, as_superadmin):
    me = _owner(client, made)
    _run(_q("UPDATE organizations SET comp_until = NOW() - INTERVAL '1 day', paid_until = NULL, "
            "features = COALESCE(features, '{}'::jsonb) || '{\"bot_active\": false}'::jsonb WHERE id = $1",
            me["org_id"], fetch="none"))
    flags = client.get(f"/api/internal/hq/orgs/{me['org_id']}").json()["flags"]
    assert [f["code"] for f in flags[:2]] == ["suspended", "paused"]
    assert all(f["severity"] == "critical" for f in flags[:2])


def test_only_mesio_superadmin_reads_a_ficha(client, made):
    me = _owner(client, made)
    assert client.get(f"/api/internal/hq/orgs/{me['org_id']}").status_code == 401
    assert client.get(f"/api/internal/hq/orgs/{me['org_id']}", headers=me["headers"]).status_code == 403


def test_unknown_org_is_404(client, as_superadmin):
    assert client.get("/api/internal/hq/orgs/987654321").status_code == 404


def test_marking_a_round_listo_stamps_ready_at_once(client, made):
    me = _owner(client, made)
    oid = _round(me["org_id"], me["location_id"], 12, "recibido", 10000)

    def mark(status):
        r = client.post(f"/api/table-orders/{oid}/status", headers=me["headers"], json={"status": status})
        assert r.status_code == 200, r.text

    mark("listo")
    first = _run(_q("SELECT ready_at FROM table_orders WHERE id = $1", oid, org_id=me["org_id"]))["ready_at"]
    assert first is not None
    mark("entregado")
    mark("listo")
    again = _run(_q("SELECT ready_at FROM table_orders WHERE id = $1", oid, org_id=me["org_id"]))["ready_at"]
    assert again == first, "ready_at is the FIRST time it was ready"
