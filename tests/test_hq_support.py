"""
Mesio HQ wave 3 — support actions (routes/internal/hq_support.py).

Each action does exactly what it says at the org (and sede) it names, never
touches another restaurant (ids collide on purpose), needs a reason, writes
ONE hq_audit_log row with that reason, and only Mesio's superadmin may call
it. Real signup, real rows, real DB.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.main import app
from app.routes.deps import verify_superadmin
from tests.test_walkthrough_2026_10_01 import (  # noqa: F401 — fixtures
    TEST_DB_URL, _owner, _q, _run, _table, client, made,
)

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")

REASON = "El dueño llamó: la mesa quedó abierta desde ayer"


@pytest.fixture
def hq():
    app.dependency_overrides[verify_superadmin] = lambda: None
    yield
    app.dependency_overrides.pop(verify_superadmin, None)


def _audit(org_id: int, action: str) -> list[dict]:
    rows = _run(_q("SELECT * FROM hq_audit_log WHERE org_id = $1 AND action = $2", org_id, f"support.{action}", fetch="all"))
    for r in rows:
        if isinstance(r["payload"], str):
            r["payload"] = json.loads(r["payload"])
    return rows


def _post(client, org_id, action, body):
    return client.post(f"/api/internal/hq/support/{org_id}/{action}", json=body)


def _sitting(org, loc, table_id) -> int:
    return _run(_q("INSERT INTO table_sessions (phone, table_id, table_name, status, org_id, location_id, started_at) "
                   "VALUES ($1, $2, '4', 'active', $3, $4, NOW() - INTERVAL '20 hours') RETURNING id",
                   f"web:{uuid.uuid4().hex[:8]}", table_id, org, loc, fetch="val", org_id=org))


def test_close_a_stuck_sitting_and_never_another_restaurants(client, made, hq):
    me, other = _owner(client, made), _owner(client, made)
    mine = _sitting(me["org_id"], me["location_id"], _table(me["org_id"], me["location_id"]))
    theirs = _sitting(other["org_id"], other["location_id"], _table(other["org_id"], other["location_id"]))

    listed = client.get(f"/api/internal/hq/orgs/{me['org_id']}/sedes/{me['location_id']}/support").json()
    assert [s["id"] for s in listed["sittings"]] == [mine]

    # Another restaurant's sitting id through my org: nothing happens.
    assert _post(client, me["org_id"], "close-sitting", {"session_id": theirs, "reason": REASON}).status_code == 404
    assert _run(_q("SELECT status FROM table_sessions WHERE id = $1", theirs, org_id=other["org_id"]))["status"] == "active"

    resp = _post(client, me["org_id"], "close-sitting", {"session_id": mine, "reason": REASON})
    assert resp.status_code == 200, resp.text
    row = _run(_q("SELECT status, closed_by FROM table_sessions WHERE id = $1", mine, org_id=me["org_id"]))
    assert row == {"status": "closed", "closed_by": "mesio_support"}
    audit = _audit(me["org_id"], "close_sitting")
    assert len(audit) == 1, "one action, one audit row (the generic middleware skips support paths)"
    assert audit[0]["payload"]["reason"] == REASON and audit[0]["target_id"] == str(mine)


def test_cancel_hung_round_and_web_order(client, made, hq):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    rid = f"r-{uuid.uuid4().hex[:8]}"
    _run(_q("INSERT INTO table_orders (id, table_id, table_name, phone, items, status, total, base_order_id, sub_number, "
            "station, branch_id, org_id, location_id, created_at) VALUES ($1, 't', '4', 'web:x', '[]'::jsonb, 'recibido', "
            "20000, $1, 1, 'all', $2::int, $3, $2::bigint, NOW() - INTERVAL '2 hours')", rid, loc, org, fetch="none", org_id=org))
    wid = f"o-{uuid.uuid4().hex[:8]}"
    _run(_q("INSERT INTO orders (id, phone, items, order_type, subtotal, total, org_id, location_id, status, created_at) "
            "VALUES ($1, '+573000000000', '[]'::jsonb, 'recoger', 30000, 30000, $2, $3, 'en_preparacion', "
            "NOW() - INTERVAL '3 hours')", wid, org, loc, fetch="none", org_id=org))

    listed = client.get(f"/api/internal/hq/orgs/{org}/sedes/{loc}/support").json()
    assert [r["id"] for r in listed["stuck_rounds"]] == [rid]
    assert [o["id"] for o in listed["stuck_web_orders"]] == [wid]

    assert _post(client, org, "cancel-round", {"order_id": rid, "reason": REASON}).status_code == 200
    assert _post(client, org, "cancel-round", {"order_id": rid, "reason": REASON}).status_code == 404  # already cancelled
    assert _run(_q("SELECT status FROM table_orders WHERE id = $1", rid, org_id=org))["status"] == "cancelled"

    assert _post(client, org, "cancel-web-order", {"order_id": wid, "reason": REASON}).status_code == 200
    web = _run(_q("SELECT status, cancelled_reason, total FROM orders WHERE id = $1", wid, org_id=org))
    assert web["status"] == "cancelado" and REASON in web["cancelled_reason"]
    assert web["total"] == 30000, "money is never touched"


def test_dismiss_old_alerts_and_clear_sold_out_only_at_that_sede(client, made, hq):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    norte = _run(_q("INSERT INTO locations (org_id, name, active) VALUES ($1, 'Norte', TRUE) RETURNING id", org, fetch="val"))
    for sede, age in ((loc, "3 hours"), (loc, "5 minutes"), (norte, "3 hours")):
        _run(_q(f"INSERT INTO waiter_alerts (phone, org_id, location_id, created_at) VALUES ('web:a', $1, $2, NOW() - INTERVAL '{age}')",
                org, sede, fetch="none", org_id=org))
    for sede, dish in ((loc, "Arepa"), (loc, "Jugo"), (norte, "Arepa")):
        _run(_q("INSERT INTO menu_availability (org_id, location_id, dish_name, available) VALUES ($1, $2, $3, FALSE)",
                org, sede, dish, fetch="none", org_id=org))

    r = _post(client, org, "dismiss-alerts", {"location_id": loc, "older_than_minutes": 60, "reason": REASON})
    assert r.json()["dismissed"] == 1
    open_left = _run(_q("SELECT location_id, COUNT(*) AS n FROM waiter_alerts WHERE org_id = $1 AND NOT dismissed "
                        "GROUP BY location_id ORDER BY location_id", org, fetch="all", org_id=org))
    assert {r["location_id"]: r["n"] for r in open_left} == {loc: 1, norte: 1}

    assert _post(client, org, "clear-sold-out", {"location_id": loc, "dish_name": "Jugo", "reason": REASON}).json()["cleared"] == 1
    assert _post(client, org, "clear-sold-out", {"location_id": loc, "dish_name": None, "reason": REASON}).json()["cleared"] == 1
    still = _run(_q("SELECT location_id, dish_name FROM menu_availability WHERE org_id = $1 AND NOT available",
                    org, fetch="all", org_id=org))
    assert still == [{"location_id": norte, "dish_name": "Arepa"}]

    other = _owner(client, made)
    r = _post(client, org, "clear-sold-out", {"location_id": other["location_id"], "dish_name": None, "reason": REASON})
    assert r.status_code == 404, "a sede of another restaurant is refused"


def test_reask_ops_keeps_the_answers_and_unpause(client, made, hq):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    _run(_q("UPDATE locations SET ops_config = $1::jsonb WHERE id = $2",
            json.dumps({"configured": True, "bar": False, "delivery": True, "courier": False, "waiter": True}), loc, fetch="none"))
    assert _post(client, org, "reask-ops", {"location_id": loc, "reason": REASON}).status_code == 200
    cfg = _run(_q("SELECT ops_config FROM locations WHERE id = $1", loc))["ops_config"]
    cfg = cfg if isinstance(cfg, dict) else json.loads(cfg)
    assert cfg["configured"] is False and cfg["delivery"] is True and cfg["waiter"] is True

    _run(_q("UPDATE organizations SET features = COALESCE(features, '{}'::jsonb) || '{\"bot_active\": false}'::jsonb WHERE id = $1",
            org, fetch="none"))
    assert _post(client, org, "unpause", {"reason": REASON}).status_code == 200
    feats = _run(_q("SELECT features FROM organizations WHERE id = $1", org))["features"]
    feats = feats if isinstance(feats, dict) else json.loads(feats)
    assert feats["bot_active"] is True


def test_close_sessions_of_a_user_and_of_a_staff_member(client, made, hq):
    me, other = _owner(client, made), _owner(client, made)
    org = me["org_id"]
    owner = _run(_q("SELECT username FROM users WHERE org_id = $1", org))["username"]
    staff_id = _run(_q("INSERT INTO staff (name, username, org_id, location_id) VALUES ('Ana Mesera', $1, $2, $3) RETURNING id::text",
                       f"ana.{uuid.uuid4().hex[:6]}", org, me["location_id"], fetch="val", org_id=org))
    _run(_q("INSERT INTO sessions (username, token_hash, expires_at) VALUES ($1, $2, NOW() + INTERVAL '1 day')",
            f"staff:{staff_id}", uuid.uuid4().bytes, fetch="none"))  # token_hash is bytea

    other_owner = _run(_q("SELECT username FROM users WHERE org_id = $1", other["org_id"]))["username"]
    assert _post(client, org, "close-sessions", {"username": other_owner, "reason": REASON}).status_code == 404
    assert _post(client, org, "close-sessions", {"username": owner, "staff_id": staff_id, "reason": REASON}).status_code == 422

    r = _post(client, org, "close-sessions", {"username": owner, "reason": REASON})
    assert r.status_code == 200 and r.json()["sessions_closed"] >= 1
    assert client.get("/api/settings", headers=me["headers"]).status_code == 401, "the owner's token no longer works"
    assert client.get("/api/settings", headers=other["headers"]).status_code == 200, "nobody else was logged out"

    r = _post(client, org, "close-sessions", {"staff_id": staff_id, "reason": REASON})
    assert r.json()["sessions_closed"] == 1


def test_password_reset_sends_the_owner_their_own_code(client, made, hq, monkeypatch):
    from app.services import email as email_mod
    monkeypatch.setattr(email_mod, "delivers_for_real", lambda: True)  # a real provider is configured
    me = _owner(client, made)
    org = me["org_id"]
    owner = _run(_q("SELECT username FROM users WHERE org_id = $1", org))["username"]
    before = _run(_q("SELECT password_hash FROM users WHERE username = $1", owner))["password_hash"]

    r = _post(client, org, "password-reset", {"username": owner, "reason": REASON})
    assert r.status_code == 200, r.text
    assert r.json()["sent_to"] == owner
    token = _run(_q("SELECT used_at FROM password_reset_tokens WHERE user_username = $1 ORDER BY id DESC LIMIT 1", owner))
    assert token and token["used_at"] is None, "a fresh code exists"
    assert _run(_q("SELECT password_hash FROM users WHERE username = $1", owner))["password_hash"] == before, \
        "Mesio never sets the password"
    assert _audit(org, "password_reset")[0]["payload"]["email_sent"] is True

    # The old endpoint that let Mesio type a new password is gone.
    old = client.post(f"/api/internal/admin/users/{owner}/reset-password", json={"new_password": "otraClave123"})
    assert old.status_code in (404, 405)


def test_every_action_needs_a_reason_and_superadmin(client, made, hq):
    me = _owner(client, made)
    r = _post(client, me["org_id"], "unpause", {"reason": "corto"})
    assert r.status_code == 422
    app.dependency_overrides.pop(verify_superadmin, None)
    r = client.post(f"/api/internal/hq/support/{me['org_id']}/unpause", json={"reason": REASON}, headers=me["headers"])
    assert r.status_code == 403
    assert _audit(me["org_id"], "unpause") == []


def test_password_reset_says_so_when_email_is_not_configured(client, made, hq):
    """Console backend = the code is only logged: the HQ must not claim it was sent."""
    me = _owner(client, made)
    owner = _run(_q("SELECT username FROM users WHERE org_id = $1", me["org_id"]))["username"]
    r = _post(client, me["org_id"], "password-reset", {"username": owner, "reason": REASON})
    assert r.status_code == 502
    assert "RESEND_API_KEY" in r.json()["detail"]
    assert _audit(me["org_id"], "password_reset")[0]["payload"]["email_sent"] is False
