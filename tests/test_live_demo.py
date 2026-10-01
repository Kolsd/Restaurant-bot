"""
tests/test_live_demo.py — /demo against the real test DB.

The live demo (app/services/live_demo.py) is an ordinary tenant that creates
itself on the first visit. These tests drive it the way a prospect does:
get a table, open the QR's session, send an order by tapping, watch it on
the demo kitchen and mark it ready.
"""
import asyncio
import os

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _close_demo_sessions_async() -> None:
    """Free every demo table so each test starts from an empty restaurant."""
    from app.services import live_demo

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            "UPDATE table_sessions SET status='closed', closed_at=NOW(), closed_by='test' "
            "WHERE table_id = ANY($1::text[]) AND status IN ('active','nps_pending')",
            live_demo.table_ids(),
        )
    finally:
        await conn.close()


async def _demo_org_async() -> dict:
    from app.services import live_demo

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        org = await conn.fetchrow(
            "SELECT id, plan_code, comp_until, paid_until FROM organizations WHERE slug = $1",
            live_demo.DEMO_SLUG,
        )
        tables = await conn.fetchval(
            "SELECT COUNT(*) FROM restaurant_tables WHERE org_id = $1 AND active", org["id"],
        )
        return {**dict(org), "tables": tables}
    finally:
        await conn.close()


class _FreshPoolClient:
    """TestClient opens a fresh event loop per call while the app's pool is
    cached; reset it before every request (same as test_diner_routes.py)."""

    def __init__(self, client):
        self._client = client

    def _call(self, method, *args, **kwargs):
        from app.services import database as db_mod
        db_mod._pool = None
        return getattr(self._client, method)(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._call("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call("post", *args, **kwargs)


@pytest.fixture
def demo_client(client):
    _run(_close_demo_sessions_async())
    yield _FreshPoolClient(client)
    _run(_close_demo_sessions_async())


def test_the_demo_restaurant_creates_itself_on_the_plan_without_ai(demo_client):
    from app.services import live_demo, plans

    resp = demo_client.post("/api/demo/table")
    assert resp.status_code == 200, resp.text
    table = resp.json()
    assert table["table_id"] in live_demo.table_ids()
    assert table["chat_url"] == f"/chat/{table['table_id']}"

    org = _run(_demo_org_async())
    assert org["plan_code"] == "esencial", "a demo must never cost an LLM call"
    assert not plans.has_feature(org, plans.AI_ASSISTANT)
    assert plans.is_open(org)
    assert org["tables"] == live_demo.DEMO_TABLES

    # Idempotent: a second visit reuses the same restaurant.
    assert demo_client.post("/api/demo/table").status_code == 200
    assert _run(_demo_org_async())["id"] == org["id"]


def test_an_order_from_the_phone_reaches_the_demo_kitchen_and_can_be_marked_ready(demo_client):
    table = demo_client.post("/api/demo/table").json()
    assert demo_client.get(f"/api/demo/kitchen/{table['table_id']}").json() == {"orders": []}

    session = demo_client.post("/api/diner/session", json={"table_id": table["table_id"]}).json()
    assert session["assistant"] is False, "the demo chat is buttons only"
    token = session["token"]
    added = demo_client.post("/api/diner/cart/add",
                             json={"token": token, "name": "Bandeja paisa", "qty": 2,
                                   "note": "Sin chicharrón"})
    assert added.status_code == 200, added.text
    sent = demo_client.post("/api/diner/order/send",
                            json={"token": token, "idempotency_key": f"demo-{token}"})
    assert sent.status_code == 200, sent.text

    orders = demo_client.get(f"/api/demo/kitchen/{table['table_id']}").json()["orders"]
    assert len(orders) == 1
    assert orders[0]["status"] == "recibido"
    assert orders[0]["items"] == [{"name": "Bandeja paisa", "qty": 2, "notes": "Sin chicharrón"}]
    assert orders[0]["created_at"].endswith("+00:00")

    resp = demo_client.post(f"/api/demo/kitchen/orders/{orders[0]['id']}", json={"status": "listo"})
    assert resp.status_code == 200
    diner_view = demo_client.get("/api/diner/table", params={"token": token}).json()
    assert [o["status"] for o in diner_view["orders"]] == ["listo"]


def test_the_demo_kitchen_only_touches_demo_tables_and_kitchen_statuses(demo_client):
    assert demo_client.get("/api/demo/kitchen/some-real-restaurant-table").status_code == 404
    assert demo_client.post("/api/demo/kitchen/orders/NOT-A-DEMO-ORDER",
                            json={"status": "listo"}).status_code == 404
    assert demo_client.post("/api/demo/kitchen/orders/ANY",
                            json={"status": "cerrar_mesa"}).status_code == 422


def test_when_every_table_is_taken_the_oldest_is_recycled(demo_client):
    from app.services import live_demo

    tokens = {}
    for table_id in live_demo.table_ids():
        resp = demo_client.post("/api/diner/session", json={"table_id": table_id})
        assert resp.status_code == 200, resp.text
        tokens[table_id] = resp.json()["token"]

    oldest = live_demo.table_ids()[0]
    table = demo_client.post("/api/demo/table").json()
    assert table["table_id"] == oldest

    # Twenty scans from one test IP used the minute's session allowance.
    from app.services import state_store
    state_store._fb_rate_limits.clear()

    # The recycled table opens fresh for the new visitor — no join code.
    resp = demo_client.post("/api/diner/session", json={"table_id": oldest})
    assert resp.status_code == 200, resp.text
    fresh = resp.json()
    assert fresh["requires_join_code"] is False
    assert fresh["token"] != tokens[oldest]
