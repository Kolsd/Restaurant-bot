"""
tests/test_menu_analytics.py

Tests for catalog v2 menu analytics — Fase 5b:
  - record_event inserts and is readable
  - get_dish_analytics calculates conversions correctly
  - get_menu_engineering_matrix classifies 4 synthetic dishes into all 4 quadrants
  - GET /api/menu/analytics requires auth
  - POST /api/public/menu/track inserts a row and respects rate limit (120/min)
  - Backward-compat: row inserted with phone=NULL and bot_number=NULL if absent
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_row, make_pool, patch_auth


client = TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _conn_with_events(events: list[dict]):
    """
    Return a mock connection whose fetchrow/fetch behave like asyncpg.
    execute() is a no-op (for INSERT).
    fetch() returns a list of make_row(d) for each dict in `events`.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetch   = AsyncMock(return_value=[make_row(e) for e in events])
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


def _pool_for(conn):
    return make_pool(conn)


# ─────────────────────────────────────────────────────────────────────────────
# 1. record_event inserts a row
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_event_inserts_row():
    from app.repositories import menu_analytics_repo

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)

    with patch.object(menu_analytics_repo, "_get_pool", return_value=_pool_for(conn)):
        await menu_analytics_repo.record_event(
            restaurant_id=1,
            dish_name="Bandeja Paisa",
            event_type="view",
            phone="573001234567",
            bot_number="5731234",
        )

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args[0]
    assert "INSERT INTO menu_events" in call_args[0]
    assert call_args[1] == 1              # restaurant_id
    assert call_args[2] == "Bandeja Paisa"
    assert call_args[3] == "view"
    assert call_args[4] == "573001234567"
    assert call_args[5] == "5731234"


# ─────────────────────────────────────────────────────────────────────────────
# 2. record_event with phone=NULL and bot_number=NULL (backward-compat)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_event_null_phone_and_bot_number():
    from app.repositories import menu_analytics_repo

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)

    with patch.object(menu_analytics_repo, "_get_pool", return_value=_pool_for(conn)):
        await menu_analytics_repo.record_event(
            restaurant_id=1,
            dish_name="Ajiaco",
            event_type="add_to_cart",
        )

    call_args = conn.execute.call_args[0]
    assert call_args[4] is None   # phone
    assert call_args[5] is None   # bot_number


# ─────────────────────────────────────────────────────────────────────────────
# 3. record_event swallows exceptions (fire-and-forget)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_event_does_not_raise_on_db_error():
    from app.repositories import menu_analytics_repo

    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=Exception("DB down"))

    with patch.object(menu_analytics_repo, "_get_pool", return_value=_pool_for(conn)):
        # Should not raise
        await menu_analytics_repo.record_event(1, "Pizza", "view")


# ─────────────────────────────────────────────────────────────────────────────
# 4. get_dish_analytics calculates conversions correctly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_dish_analytics_conversions():
    from app.repositories import menu_analytics_repo

    fake_rows = [
        make_row({
            "dish_name":    "Pizza",
            "views":        100,
            "modal_opens":  40,
            "add_to_carts": 20,
            "orders":       10,
        }),
        make_row({
            "dish_name":    "Pasta",
            "views":        0,
            "modal_opens":  0,
            "add_to_carts": 0,
            "orders":       0,
        }),
    ]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fake_rows)

    with patch.object(menu_analytics_repo, "_get_pool", return_value=_pool_for(conn)):
        result = await menu_analytics_repo.get_dish_analytics(1, 30)

    pizza = next(r for r in result if r["dish_name"] == "Pizza")
    pasta = next(r for r in result if r["dish_name"] == "Pasta")

    assert pizza["views"] == 100
    assert pizza["add_to_carts"] == 20
    assert pizza["orders"] == 10
    assert pizza["cart_conversion_rate"]  == pytest.approx(0.2, abs=1e-4)   # 20/100
    assert pizza["order_conversion_rate"] == pytest.approx(0.5, abs=1e-4)   # 10/20

    # Zero-division guard
    assert pasta["cart_conversion_rate"]  == 0.0
    assert pasta["order_conversion_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 5. get_menu_engineering_matrix classifies 4 synthetic dishes into 4 quadrants
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_menu_engineering_matrix_four_quadrants():
    """
    Synthetic data engineered so that medians split cleanly:
      Dish A (star):      views=200, orders=40  → high pop (200≥100), high conv (0.20≥0.15)
      Dish B (plowhorse): views=200, orders=10  → high pop,            low  conv (0.05<0.15)
      Dish C (puzzle):    views=50,  orders=20  → low  pop  (50<100),  high conv (0.40≥0.15)
      Dish D (dog):       views=50,  orders=5   → low  pop,            low  conv (0.10<0.15)

    median(views)      = 100  (median of [50,50,200,200])
    median(conversion) = 0.15 (median of [0.05,0.10,0.20,0.40])
    """
    from app.repositories import menu_analytics_repo

    # Stub get_dish_analytics so matrix doesn't need a real DB
    fake_per_dish = [
        {"dish_name": "A_star",      "views": 200, "modal_opens": 80, "add_to_carts": 80, "orders": 40, "cart_conversion_rate": 0.4, "order_conversion_rate": 0.5},
        {"dish_name": "B_plowhorse", "views": 200, "modal_opens": 60, "add_to_carts": 60, "orders": 10, "cart_conversion_rate": 0.3, "order_conversion_rate": 0.17},
        {"dish_name": "C_puzzle",    "views":  50, "modal_opens": 30, "add_to_carts": 30, "orders": 20, "cart_conversion_rate": 0.6, "order_conversion_rate": 0.67},
        {"dish_name": "D_dog",       "views":  50, "modal_opens": 10, "add_to_carts": 10, "orders":  5, "cart_conversion_rate": 0.2, "order_conversion_rate": 0.5},
    ]

    with patch.object(menu_analytics_repo, "get_dish_analytics", AsyncMock(return_value=fake_per_dish)):
        matrix = await menu_analytics_repo.get_menu_engineering_matrix(1, 30)

    star_names      = [d["dish_name"] for d in matrix["stars"]]
    plowhorse_names = [d["dish_name"] for d in matrix["plowhorses"]]
    puzzle_names    = [d["dish_name"] for d in matrix["puzzles"]]
    dog_names       = [d["dish_name"] for d in matrix["dogs"]]

    assert "A_star"      in star_names,      f"stars: {star_names}"
    assert "B_plowhorse" in plowhorse_names, f"plowhorses: {plowhorse_names}"
    assert "C_puzzle"    in puzzle_names,    f"puzzles: {puzzle_names}"
    assert "D_dog"       in dog_names,       f"dogs: {dog_names}"

    # All four quadrant keys always present
    assert set(matrix.keys()) == {"stars", "puzzles", "plowhorses", "dogs"}


# ─────────────────────────────────────────────────────────────────────────────
# 6. get_menu_engineering_matrix returns empty quadrants when no data
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_menu_engineering_matrix_empty():
    from app.repositories import menu_analytics_repo

    with patch.object(menu_analytics_repo, "get_dish_analytics", AsyncMock(return_value=[])):
        matrix = await menu_analytics_repo.get_menu_engineering_matrix(1, 30)

    assert matrix == {"stars": [], "puzzles": [], "plowhorses": [], "dogs": []}


# ─────────────────────────────────────────────────────────────────────────────
# 7. GET /api/menu/analytics requires auth (401 without valid token)
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_analytics_endpoint_requires_auth(monkeypatch):
    """No valid token → verify_token returns None → 401."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=None))
    resp = client.get("/api/menu/analytics", headers={"Authorization": "Bearer bad"})
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# 8. GET /api/menu/analytics returns data when authenticated
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_analytics_endpoint_authenticated(monkeypatch):
    from app.repositories import menu_analytics_repo

    patch_auth(monkeypatch, restaurant_id=1)

    monkeypatch.setattr(
        menu_analytics_repo,
        "get_dish_analytics",
        AsyncMock(return_value=[
            {"dish_name": "Empanada", "views": 10, "modal_opens": 5,
             "add_to_carts": 3, "orders": 2,
             "cart_conversion_rate": 0.3, "order_conversion_rate": 0.67},
        ])
    )
    monkeypatch.setattr(
        menu_analytics_repo,
        "get_menu_engineering_matrix",
        AsyncMock(return_value={"stars": [], "puzzles": [], "plowhorses": [], "dogs": []})
    )

    resp = client.get("/api/menu/analytics", headers={"Authorization": "Bearer testtoken"})
    assert resp.status_code == 200
    data = resp.json()
    assert "per_dish" in data
    assert "matrix"   in data
    assert data["per_dish"][0]["dish_name"] == "Empanada"


# ─────────────────────────────────────────────────────────────────────────────
# 9. GET /api/menu/analytics respects days param (max 365)
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_analytics_endpoint_days_clamp(monkeypatch):
    from app.repositories import menu_analytics_repo

    patch_auth(monkeypatch, restaurant_id=1)

    captured_days = {}

    async def fake_get_dish_analytics(restaurant_id, days):
        captured_days["days"] = days
        return []

    async def fake_matrix(restaurant_id, days):
        return {"stars": [], "puzzles": [], "plowhorses": [], "dogs": []}

    monkeypatch.setattr(menu_analytics_repo, "get_dish_analytics",    fake_get_dish_analytics)
    monkeypatch.setattr(menu_analytics_repo, "get_menu_engineering_matrix", fake_matrix)

    # days > 365 should be clamped to 365
    resp = client.get("/api/menu/analytics?days=999", headers={"Authorization": "Bearer testtoken"})
    assert resp.status_code == 200
    assert captured_days["days"] == 365


# ─────────────────────────────────────────────────────────────────────────────
# 10. POST /api/public/menu/track inserts a row (happy path)
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_track_inserts_row(monkeypatch):
    from app.repositories import menu_analytics_repo

    fake_restaurant = {"id": 42, "name": "Test Resto", "whatsapp_number": "5731234"}
    monkeypatch.setattr(
        "app.repositories.restaurant_repo.db_get_restaurant_by_bot_number",
        AsyncMock(return_value=fake_restaurant),
    )

    recorded = {}
    async def fake_record_event(restaurant_id, dish_name, event_type, phone=None, bot_number=None):
        recorded.update(locals())

    monkeypatch.setattr(menu_analytics_repo, "record_event", fake_record_event)

    resp = client.post("/api/public/menu/track", json={
        "dish_name":  "Bandeja Paisa",
        "event_type": "view",
        "bot_number": "5731234",
    })
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert recorded.get("restaurant_id") == 42
    assert recorded.get("dish_name") == "Bandeja Paisa"
    assert recorded.get("event_type") == "view"
    assert recorded.get("phone") is None


# ─────────────────────────────────────────────────────────────────────────────
# 11. POST /api/public/menu/track: unknown bot_number → 200 (no DB insert)
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_track_unknown_bot_returns_200(monkeypatch):
    from app.repositories import menu_analytics_repo

    monkeypatch.setattr(
        "app.repositories.restaurant_repo.db_get_restaurant_by_bot_number",
        AsyncMock(return_value=None),
    )

    record_called = []
    async def fake_record(*args, **kwargs):
        record_called.append(True)

    monkeypatch.setattr(menu_analytics_repo, "record_event", fake_record)

    resp = client.post("/api/public/menu/track", json={
        "dish_name":  "Tacos",
        "event_type": "add_to_cart",
        "bot_number": "9999999",
    })
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(record_called) == 0   # no insert when restaurant not found


# ─────────────────────────────────────────────────────────────────────────────
# 12. POST /api/public/menu/track: rate limit (120/min)
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_track_rate_limit(monkeypatch):
    """After 120 requests the 121st is silently dropped (still returns 200)."""
    from app.repositories import menu_analytics_repo
    from app.services import state_store

    # Reset in-process rate limit state
    state_store._fb_rate_limits.clear()

    fake_restaurant = {"id": 1, "name": "Test", "whatsapp_number": "111"}
    monkeypatch.setattr(
        "app.repositories.restaurant_repo.db_get_restaurant_by_bot_number",
        AsyncMock(return_value=fake_restaurant),
    )

    insert_count = []
    async def fake_record(*args, **kwargs):
        insert_count.append(1)

    monkeypatch.setattr(menu_analytics_repo, "record_event", fake_record)

    for _ in range(121):
        resp = client.post("/api/public/menu/track", json={
            "dish_name":  "Soup",
            "event_type": "view",
            "bot_number": "111",
        })
        assert resp.status_code == 200

    # First 120 should insert; 121st is rate-limited
    assert len(insert_count) == 120


# ─────────────────────────────────────────────────────────────────────────────
# 13. POST /api/public/menu/track: 'ordered' is a valid event_type
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_track_ordered_event_valid(monkeypatch):
    from app.repositories import menu_analytics_repo

    fake_restaurant = {"id": 1, "name": "Test", "whatsapp_number": "222"}
    monkeypatch.setattr(
        "app.repositories.restaurant_repo.db_get_restaurant_by_bot_number",
        AsyncMock(return_value=fake_restaurant),
    )
    monkeypatch.setattr(menu_analytics_repo, "record_event", AsyncMock())

    resp = client.post("/api/public/menu/track", json={
        "dish_name":  "Jugo",
        "event_type": "ordered",
        "bot_number": "222",
    })
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 14. POST /api/public/menu/track: invalid event_type → 422
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_track_invalid_event_type():
    resp = client.post("/api/public/menu/track", json={
        "dish_name":  "Agua",
        "event_type": "hack",
        "bot_number": "333",
    })
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# 15. POST /api/public/menu/track: phone forwarded to record_event when present
# ─────────────────────────────────────────────────────────────────────────────

def test_menu_track_phone_forwarded(monkeypatch):
    from app.repositories import menu_analytics_repo

    fake_restaurant = {"id": 5, "name": "Test", "whatsapp_number": "444"}
    monkeypatch.setattr(
        "app.repositories.restaurant_repo.db_get_restaurant_by_bot_number",
        AsyncMock(return_value=fake_restaurant),
    )

    recorded = {}
    async def fake_record(restaurant_id, dish_name, event_type, phone=None, bot_number=None):
        recorded.update({"phone": phone, "bot_number": bot_number})

    monkeypatch.setattr(menu_analytics_repo, "record_event", fake_record)

    resp = client.post("/api/public/menu/track", json={
        "dish_name":  "Sopa",
        "event_type": "modal_open",
        "bot_number": "444",
        "phone":      "573009876543",
    })
    assert resp.status_code == 200
    assert recorded["phone"] == "573009876543"
    assert recorded["bot_number"] == "444"
