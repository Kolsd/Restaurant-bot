"""
Mesio HQ wave 2 — platform_errors (migration 0108).

A 500 and a failed bot turn each leave a row with the restaurant and sede
they hit; the ficha shows them grouped and raises "bot_failing" when diners
keep getting "tengo un problema técnico". Real signup, real DB.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes.deps import verify_superadmin
from app.services import error_log
from tests.test_walkthrough_2026_10_01 import (  # noqa: F401 — fixtures
    TEST_DB_URL, _owner, _q, _run, _table, client, made,
)

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")


def _rows(org_id: int, *, want: int, timeout: float = 3.0) -> list[dict]:
    """platform_errors is written fire-and-forget: wait for the rows."""
    deadline = time.monotonic() + timeout
    while True:
        rows = _run(_q("SELECT * FROM platform_errors WHERE org_id = $1 ORDER BY id", org_id, fetch="all"))
        if len(rows) >= want or time.monotonic() > deadline:
            return rows
        time.sleep(0.05)


def test_an_unhandled_500_is_recorded_with_its_restaurant(made, monkeypatch):
    from app.routes import settings_routes

    with TestClient(app, raise_server_exceptions=False) as c:
        me = _owner(c, made)

        def _boom(*_a, **_kw):
            raise RuntimeError("settings exploded for +57 300 123 4567")

        monkeypatch.setattr(settings_routes, "_build_settings_response", _boom)
        resp = c.get("/api/settings", headers=me["headers"])
        assert resp.status_code == 500
        # The write is fire-and-forget on the app's loop: read it before the
        # client (and that loop) shuts down.
        rows = _rows(me["org_id"], want=1)

    assert len(rows) == 1
    row = rows[0]
    assert (row["source"], row["route"], row["method"], row["status"]) == ("http", "/api/settings", "GET", 500)
    assert row["error_type"] == "RuntimeError"
    assert "300 123 4567" not in row["message"], "phone numbers are masked"
    assert row["request_id"]


def test_a_failed_bot_turn_reaches_the_ficha_as_bot_failing(client, made, monkeypatch):
    from app.services import agent as agent_mod

    async def _llm_down(*_a, **_kw):
        raise RuntimeError("anthropic: credit balance is too low")

    monkeypatch.setattr(agent_mod, "call_claude", _llm_down)
    me = _owner(client, made)
    t = _table(me["org_id"], me["location_id"])
    token = client.post("/api/diner/session", json={"table_id": t}).json()["token"]
    for _ in range(3):
        resp = client.post("/api/diner/chat", json={"token": token, "message": "¿qué me recomiendas hoy?"})
        assert resp.status_code == 200
        assert "problema técnico" in resp.json()["message"]

    rows = _rows(me["org_id"], want=3)
    assert len(rows) == 3
    assert {r["source"] for r in rows} == {"bot"}
    assert {r["location_id"] for r in rows} == {me["location_id"]}
    assert len({r["fingerprint"] for r in rows}) == 1, "the same failure groups together"

    app.dependency_overrides[verify_superadmin] = lambda: None
    try:
        snap = client.get(f"/api/internal/hq/orgs/{me['org_id']}").json()
        overview = client.get("/api/internal/hq/errors?hours=24").json()
    finally:
        app.dependency_overrides.pop(verify_superadmin, None)
    assert snap["flags"][0]["code"] == "bot_failing" and snap["flags"][0]["count"] == 3
    assert snap["errors"][0]["count"] == 3 and snap["errors"][0]["source"] == "bot"
    mine = next(r for r in overview["by_org"] if r["org_id"] == me["org_id"])
    assert (mine["count"], mine["bot_count"]) == (3, 3)


def test_errors_of_another_restaurant_never_show_on_this_ficha(client, made):
    me, other = _owner(client, made), _owner(client, made)
    _run(_q("INSERT INTO platform_errors (source, org_id, error_type, fingerprint) VALUES ('http', $1, 'X', 'f1')",
            other["org_id"], fetch="none"))
    app.dependency_overrides[verify_superadmin] = lambda: None
    try:
        snap = client.get(f"/api/internal/hq/orgs/{me['org_id']}").json()
    finally:
        app.dependency_overrides.pop(verify_superadmin, None)
    assert snap["errors"] == []


def test_only_superadmin_reads_the_error_log(client, made):
    me = _owner(client, made)
    assert client.get("/api/internal/hq/errors", headers=me["headers"]).status_code == 403


def test_fingerprint_ignores_ids_and_messages_are_masked():
    a = error_log.fingerprint("http", "/api/orders/123", "KeyError", "missing 4471: x")
    b = error_log.fingerprint("http", "/api/orders/987", "KeyError", "missing 12: y")
    assert a == b
    assert error_log.fingerprint("bot", "/api/orders/1", "KeyError", "missing") != a
    assert "3001234567" not in error_log.clean_message("llamar a 3001234567 ya")
    assert len(error_log.clean_message("x" * 2000)) == 500
