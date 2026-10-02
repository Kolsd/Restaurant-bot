"""
Mesio HQ wave 4 — alert rules (services/hq_alerts.py, migration 0109).

A health flag opens an alert, the next runs keep the SAME alert, it resolves
on its own when the problem is gone; critical alerts are emailed once, and
never "sent" through the console backend. Real signup, real DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.main import app
from app.routes.deps import verify_superadmin
from app.services import hq_snapshot
from tests.test_walkthrough_2026_10_01 import (  # noqa: F401 — fixtures
    TEST_DB_URL, _owner, _q, _run, client, made,
)

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")


@pytest.fixture
def hq():
    app.dependency_overrides[verify_superadmin] = lambda: None
    yield
    app.dependency_overrides.pop(verify_superadmin, None)


@pytest.fixture
def outbox(monkeypatch):
    """A real provider is configured; capture what would be delivered."""
    from app.services import email as email_mod
    sent: list[dict] = []

    async def _send(to, subject, html, text=None):
        sent.append({"to": to, "subject": subject, "html": html, "text": text})
        return True

    monkeypatch.setattr(email_mod, "delivers_for_real", lambda: True)
    monkeypatch.setattr(email_mod, "send_email", _send)
    return sent


def _stuck_round(org, loc) -> str:
    rid = f"r-{uuid.uuid4().hex[:8]}"
    _run(_q("INSERT INTO table_orders (id, table_id, table_name, phone, items, status, total, base_order_id, sub_number, "
            "station, branch_id, org_id, location_id, created_at) VALUES ($1, 't', '4', 'web:x', '[]'::jsonb, 'recibido', "
            "20000, $1, 1, 'all', $2::int, $3, $2::bigint, NOW() - INTERVAL '2 hours')", rid, loc, org, fetch="none", org_id=org))
    return rid


def _alerts(org_id: int, code: str) -> list[dict]:
    return _run(_q("SELECT * FROM hq_alerts WHERE org_id = $1 AND code = $2 ORDER BY id", org_id, code, fetch="all"))


def _run_rules(client):
    r = client.post("/api/internal/hq/alerts/run")
    assert r.status_code == 200, r.text
    return r.json()


def test_an_alert_opens_once_is_emailed_once_and_resolves_on_its_own(client, made, hq, outbox, monkeypatch):
    monkeypatch.setenv("HQ_ALERT_EMAIL", "miguel@mesioai.com")
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    rid = _stuck_round(org, loc)

    _run_rules(client)
    opened = _alerts(org, "stuck_rounds")
    assert len(opened) == 1 and opened[0]["status"] == "open" and opened[0]["severity"] == "critical"
    assert opened[0]["location_id"] == loc
    mine = [m for m in outbox if f"/internal/org/{org}" in m["html"]]
    assert len(mine) == 1 and mine[0]["to"] == "miguel@mesioai.com"
    assert "Cómo resolverlo" in mine[0]["text"]

    _run_rules(client)  # still stuck
    again = _alerts(org, "stuck_rounds")
    assert [a["id"] for a in again] == [opened[0]["id"]], "the same alert stays open, no duplicate"
    assert len([m for m in outbox if f"/internal/org/{org}" in m["html"]]) == 1, "emailed once, not every run"

    _run(_q("UPDATE table_orders SET status = 'listo' WHERE id = $1", rid, fetch="none", org_id=org))
    _run_rules(client)
    closed = _alerts(org, "stuck_rounds")
    assert closed[0]["status"] == "resolved" and closed[0]["resolved_at"] is not None

    listed = client.get(f"/api/internal/hq/alerts?org_id={org}").json()
    assert listed["alerts"][0]["code"] == "stuck_rounds" and listed["alerts"][0]["fix"]
    assert listed["email"] == {"to": "miguel@mesioai.com", "configured": True}


def test_without_a_real_email_provider_critical_alerts_stay_pending(client, made, hq, monkeypatch):
    from app.services import email as email_mod
    calls: list = []

    async def _send(*a, **kw):
        calls.append((a, kw))
        return True

    monkeypatch.setattr(email_mod, "send_email", _send)  # console-like: would "succeed"
    me = _owner(client, made)
    _stuck_round(me["org_id"], me["location_id"])
    _run_rules(client)
    alert = _alerts(me["org_id"], "stuck_rounds")[0]
    assert alert["emailed_at"] is None, "not claimed as sent: it goes out once email is configured"
    assert not [c for c in calls if "Mesio HQ" in str(c)], "no alert digest through a backend that only logs"
    assert client.get("/api/internal/hq/alerts").json()["email"]["configured"] is False


def test_warnings_open_but_are_not_emailed_and_info_never_opens(client, made, hq, outbox):
    me = _owner(client, made)
    org = me["org_id"]
    _run(_q("INSERT INTO table_sessions (phone, table_id, table_name, status, org_id, location_id, started_at) "
            "VALUES ('web:s', 't-old', '4', 'active', $1, $2, NOW() - INTERVAL '8 hours')", org, me["location_id"],
            fetch="none", org_id=org))
    _run_rules(client)
    assert _alerts(org, "sittings_over_6h")[0]["severity"] == "warning"
    assert _alerts(org, "no_tables") == [], "info flags stay on the ficha only"
    assert not [m for m in outbox if f"/internal/org/{org}" in m["html"]]


def test_open_alerts_reach_the_hq_inbox_with_a_link_to_the_ficha(client, made, hq, outbox):
    me = _owner(client, made)
    _stuck_round(me["org_id"], me["location_id"])
    _run_rules(client)
    items = client.get("/api/internal/notifications").json()["items"]
    mine = [n for n in items if n.get("type") == "hq_alert" and n.get("tenant_id") == me["org_id"]]
    assert mine and mine[0]["url"] == f"/internal/org/{me['org_id']}" and mine[0]["severity"] == "critical"


def test_quiet_during_hours_fires_only_for_a_sede_that_normally_sells(client, made, hq, outbox, monkeypatch):
    monkeypatch.setattr(hq_snapshot, "open_long_enough", lambda *_a, **_kw: True)
    busy, quiet_new = _owner(client, made), _owner(client, made)
    for i in range(10):  # 10 rounds this week, the last one 5 hours ago
        _run(_q("INSERT INTO table_orders (id, table_id, table_name, phone, items, status, total, base_order_id, sub_number, "
                "station, branch_id, org_id, location_id, created_at) VALUES ($1, 't', '4', 'web:x', '[]'::jsonb, 'entregado', "
                "10000, $1, 1, 'all', $2::int, $3, $2::bigint, NOW() - make_interval(hours => 5 + $4))",
                f"q-{uuid.uuid4().hex[:8]}", busy["location_id"], busy["org_id"], i * 10, fetch="none", org_id=busy["org_id"]))
    _run_rules(client)
    assert _alerts(busy["org_id"], "quiet_during_hours")[0]["severity"] == "critical"
    assert _alerts(quiet_new["org_id"], "quiet_during_hours") == [], "a sede with no sales history is not 'quiet'"


def test_open_long_enough_reads_the_sede_hours():
    bogota = ZoneInfo("America/Bogota")
    hours = {"thursday": {"open": "12:00", "close": "22:00", "closed": False},
             "friday": {"open": "18:00", "close": "02:00", "closed": False},
             "sunday": {"closed": True}}

    def at(y, m, d, h, mi=0):
        return datetime(y, m, d, h, mi, tzinfo=bogota).astimezone(timezone.utc)

    # 2026-10-01 is a Thursday.
    assert hq_snapshot.open_long_enough(hours, "America/Bogota", at(2026, 10, 1, 16)) is True
    assert hq_snapshot.open_long_enough(hours, "America/Bogota", at(2026, 10, 1, 14)) is False  # open < 3 h
    assert hq_snapshot.open_long_enough(hours, "America/Bogota", at(2026, 10, 1, 23)) is False  # closed
    assert hq_snapshot.open_long_enough(hours, "America/Bogota", at(2026, 10, 2, 23, 30)) is True  # closes after midnight
    assert hq_snapshot.open_long_enough(hours, "America/Bogota", at(2026, 10, 4, 15)) is False  # sunday closed
    assert hq_snapshot.open_long_enough({}, "America/Bogota", at(2026, 10, 1, 16)) is False  # unknown hours
