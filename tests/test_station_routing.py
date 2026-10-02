"""
Tests for PHASE 2: Multi-station Kitchen / Bar routing.
Covers: ?station= filter on GET /api/table-orders, /bar route,
       station in db_save_table_order, split logic in agent.execute_action.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import app.routes.tables as tables_routes
import app.services.agent as agent_module


# ── Shared fixtures ──────────────────────────────────────────────

@pytest.fixture
def mock_auth(monkeypatch):
    """An owner of org 1, with no sede picked — the org-wide view.

    `org_id` is not decoration: /api/table-orders refuses a caller whose org
    cannot be resolved, because the repo's "admin with no filter at all"
    branch runs under bypass_tenant_scope and would return EVERY tenant's
    table orders. This fixture used to leave org_id unset and the endpoint
    answered 200 with exactly that cross-tenant result.
    """
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(
        "app.routes.deps.db.db_get_user",
        AsyncMock(return_value={
            "username": "admin", "restaurant_name": "Test",
            "branch_id": None, "org_id": 1, "location_id": None, "role": "owner",
        }),
    )
    monkeypatch.setattr(
        "app.routes.deps.db.db_get_restaurant_by_org_id",
        AsyncMock(return_value={"id": 1, "org_id": 1, "location_id": None,
                                "name": "Test"}),
    )


SAMPLE_ORDERS = [
    {"id": "MESA-K1",   "table_name": "Mesa 1", "status": "recibido",       "station": "kitchen", "items": [], "created_at": "2024-06-15T12:00:00Z", "updated_at": "2024-06-15T12:00:00Z"},
    {"id": "MESA-B1",   "table_name": "Mesa 1", "status": "recibido",       "station": "bar",     "items": [], "created_at": "2024-06-15T12:00:00Z", "updated_at": "2024-06-15T12:00:00Z"},
    {"id": "MESA-ALL1", "table_name": "Mesa 2", "status": "en_preparacion", "station": "all",     "items": [], "created_at": "2024-06-15T12:01:00Z", "updated_at": "2024-06-15T12:01:00Z"},
    {"id": "MESA-K2",   "table_name": "Mesa 3", "status": "listo",          "station": "kitchen", "items": [], "created_at": "2024-06-15T12:02:00Z", "updated_at": "2024-06-15T12:02:00Z"},
]


# ══════════════════════════════════════════════════════════════════════
# 1. ?station= filter on GET /api/table-orders
# ══════════════════════════════════════════════════════════════════════

def test_table_orders_without_filter_returns_all(client, mock_auth, monkeypatch):
    """/api/table-orders without ?station returns all orders (for the Cashier)."""
    async def mock_fetch(*args, **kwargs):
        class FakeConn:
            async def fetch(self, *a, **k):
                return [MagicMock(**{**o, "__iter__": lambda s: iter(o.items()), "keys": lambda s: o.keys(), "__getitem__": lambda s, k: o[k]}) for o in SAMPLE_ORDERS]
            async def fetchval(self, *a, **k): return None
            async def execute(self, *a, **k): pass
            def transaction(self):
                class _T:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *a): pass
                return _T()
        return FakeConn()

    class FakePool:
        def acquire(self): return self
        async def __aenter__(self):
            class FakeConn:
                async def fetch(self, *a, **k): return []
                async def fetchval(self, *a, **k): return None
                async def execute(self, *a, **k): pass
                def transaction(self):
                    class _T:
                        async def __aenter__(self): return self
                        async def __aexit__(self, *a): pass
                    return _T()
            return FakeConn()
        async def __aexit__(self, *a): pass

    # Direct mock of the pool and the query
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=FakePool()))

    headers = {"Authorization": "Bearer token"}
    response = client.get("/api/table-orders", headers=headers)
    # Without a real DB we just verify the endpoint responds and has the "orders" key
    assert response.status_code == 200
    assert "orders" in response.json()


def _make_pool_with_orders(orders: list):
    """Creates a pool mock that returns the given orders as dicts with attributes."""
    class FakeRow(dict):
        def keys(self): return super().keys()
        def __iter__(self): return iter(self.items())

    class FakeConn:
        async def fetch(self, *a, **k):
            return [FakeRow(o) for o in orders]
        async def fetchval(self, *a, **k): return None  # set_config calls
        async def execute(self, *a, **k): pass

        def transaction(self):
            class _FakeTxn:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
            return _FakeTxn()

    class FakePool:
        def acquire(self): return self
        async def __aenter__(self): return FakeConn()
        async def __aexit__(self, *a): pass

    return FakePool()


def test_filter_station_kitchen_excludes_bar(client, mock_auth, monkeypatch):
    """?station=kitchen must return only station='kitchen' and station='all'."""
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=_make_pool_with_orders(SAMPLE_ORDERS)))
    headers = {"Authorization": "Bearer token"}
    response = client.get("/api/table-orders?station=kitchen", headers=headers)
    assert response.status_code == 200
    orders = response.json()["orders"]
    stations = {o["station"] for o in orders}
    assert "bar" not in stations
    assert stations <= {"kitchen", "all"}
    ids = {o["id"] for o in orders}
    assert "MESA-K1" in ids
    assert "MESA-ALL1" in ids
    assert "MESA-B1" not in ids


def test_filter_station_bar_excludes_kitchen(client, mock_auth, monkeypatch):
    """?station=bar must return only station='bar' and station='all'."""
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=_make_pool_with_orders(SAMPLE_ORDERS)))
    headers = {"Authorization": "Bearer token"}
    response = client.get("/api/table-orders?station=bar", headers=headers)
    assert response.status_code == 200
    orders = response.json()["orders"]
    stations = {o["station"] for o in orders}
    assert "kitchen" not in stations
    assert stations <= {"bar", "all"}
    ids = {o["id"] for o in orders}
    assert "MESA-B1" in ids
    assert "MESA-ALL1" in ids
    assert "MESA-K1" not in ids
    assert "MESA-K2" not in ids


def test_filter_station_all_returns_all(client, mock_auth, monkeypatch):
    """Without ?station= all records pass the filter."""
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=_make_pool_with_orders(SAMPLE_ORDERS)))
    headers = {"Authorization": "Bearer token"}
    response = client.get("/api/table-orders", headers=headers)
    assert response.status_code == 200
    orders = response.json()["orders"]
    assert len(orders) == len(SAMPLE_ORDERS)


# ══════════════════════════════════════════════════════════════════════
# 2. Bar/Kitchen are now sections of the unified Staff App (/staff) —
#    the old dedicated /bar and /kitchen pages are removed with no
#    redirect (Staff App unification, 2026-09-14). See
#    tests/test_staff_app.py for the /staff shell + role->section tests.
# ══════════════════════════════════════════════════════════════════════

def test_bar_route_removed(client):
    """/bar must be gone — Bar is now a section inside /staff, not its own page."""
    response = client.get("/bar")
    assert response.status_code == 404


def test_kitchen_route_removed(client):
    """/kitchen must be gone — Kitchen is now a section inside /staff, not its own page."""
    response = client.get("/kitchen")
    assert response.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# 3. ManualOrderRequest accepts station and passes it to DB
# ══════════════════════════════════════════════════════════════════════

def test_pos_order_station_default_all(client, monkeypatch):
    """POST /api/pos/order without station= must use station='all'."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "get_current_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "org_id": 1, "location_id": 1, "role": "owner"}))
    mock_save = AsyncMock()
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_by_id", AsyncMock(side_effect=lambda tid: {"id": tid, "org_id": 1, "location_id": 1}))
    monkeypatch.setattr(tables_routes.db, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tables_routes.db, "db_save_table_order", mock_save)
    monkeypatch.setattr(tables_routes.db, "db_get_next_sub_number", AsyncMock(return_value=1))

    payload = {
        "table_id":   "mesa-1",
        "table_name": "Mesa 1",
        "items":      [{"name": "Pizza", "price": 35000, "quantity": 1}],
        "total":      35000,
        "notes":      "",
        # station not specified → must use 'all'
    }
    response = client.post("/api/pos/order", json=payload, headers={"Authorization": "Bearer token"})
    assert response.status_code == 200

    saved = mock_save.call_args[0][0]
    assert saved["station"] == "all"


def test_pos_order_station_bar(client, monkeypatch):
    """POST /api/pos/order with station='bar' must save it as 'bar'."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "get_current_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "org_id": 1, "location_id": 1, "role": "owner"}))
    mock_save = AsyncMock()
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_by_id", AsyncMock(side_effect=lambda tid: {"id": tid, "org_id": 1, "location_id": 1}))
    monkeypatch.setattr(tables_routes.db, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tables_routes.db, "db_save_table_order", mock_save)
    monkeypatch.setattr(tables_routes.db, "db_get_next_sub_number", AsyncMock(return_value=1))

    payload = {
        "table_id":   "mesa-2",
        "table_name": "Mesa 2",
        "items":      [{"name": "Mojito", "price": 25000, "quantity": 2}],
        "total":      50000,
        "station":    "bar",
    }
    response = client.post("/api/pos/order", json=payload, headers={"Authorization": "Bearer token"})
    assert response.status_code == 200
    assert "bar" in response.json()["message"]

    saved = mock_save.call_args[0][0]
    assert saved["station"] == "bar"


def test_pos_order_station_kitchen_message(client, monkeypatch):
    """POST /api/pos/order with station='kitchen' gives a kitchen message."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "get_current_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "org_id": 1, "location_id": 1, "role": "owner"}))
    monkeypatch.setattr(tables_routes.db, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tables_routes.db, "db_save_table_order", AsyncMock())
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_by_id", AsyncMock(side_effect=lambda tid: {"id": tid, "org_id": 1, "location_id": 1}))
    monkeypatch.setattr(tables_routes.db, "db_get_next_sub_number", AsyncMock(return_value=1))

    payload = {
        "table_id":   "mesa-3",
        "table_name": "Mesa 3",
        "items":      [{"name": "Hamburguesa", "price": 35000, "quantity": 1}],
        "total":      35000,
        "station":    "kitchen",
    }
    response = client.post("/api/pos/order", json=payload, headers={"Authorization": "Bearer token"})
    assert response.status_code == 200
    assert "cocina" in response.json()["message"]


# ══════════════════════════════════════════════════════════════════════
# 4. Split logic in execute_action (agent.py)
# ══════════════════════════════════════════════════════════════════════

MOCK_CART_MIXED = {
    "items": [
        {"name": "Pizza",  "price": 35000, "quantity": 1, "subtotal": 35000, "category": "Comidas"},
        {"name": "Mojito", "price": 25000, "quantity": 1, "subtotal": 25000, "category": "Bebidas"},
        {"name": "Agua",   "price": 5000,  "quantity": 2, "subtotal": 10000, "category": "Bebidas"},
    ]
}

MOCK_RESTAURANT_BAR = {
    "id": 1,
    "features": {
        "bar_enabled":    True,
        "bar_categories": ["Bebidas", "Licores", "Cócteles"],
    }
}

MOCK_RESTAURANT_NO_BAR = {
    "id": 1,
    "features": {"bar_enabled": False}
}

MOCK_TABLE = {"id": "mesa-1", "name": "Mesa 1"}


@pytest.mark.asyncio
async def test_execute_action_split_kitchen_and_bar():
    """With bar_enabled=True, mixed items create two sub-orders: kitchen and bar."""
    saved_orders = []

    async def fake_save(order):
        saved_orders.append(order)

    with (
        patch.object(agent_module.db, "db_get_cart", AsyncMock(return_value=MOCK_CART_MIXED)),
        patch.object(agent_module.orders, "get_cart_total", AsyncMock(return_value=70000)),
        patch.object(agent_module.db, "db_get_base_order_id", AsyncMock(return_value=None)),
        patch.object(agent_module.db, "db_get_restaurant_by_org_id", AsyncMock(return_value=MOCK_RESTAURANT_BAR)),
        patch.object(agent_module.db, "db_save_table_order", AsyncMock(side_effect=fake_save)),
        patch.object(agent_module.db, "db_get_next_sub_number", AsyncMock(return_value=2)),
        patch.object(agent_module.db, "db_deduct_inventory_for_order", AsyncMock()),
        patch.object(agent_module.orders, "clear_cart", AsyncMock()),
        patch.object(agent_module.db, "db_session_mark_order", AsyncMock()),
        patch.object(agent_module.db, "get_pool", AsyncMock(return_value=MagicMock(
            acquire=MagicMock(return_value=MagicMock(
                __aenter__=AsyncMock(return_value=MagicMock(execute=AsyncMock())),
                __aexit__=AsyncMock(return_value=None),
            ))
        ))),
    ):
        parsed = {"action": "order", "items": [], "reply": "Pedido recibido"}
        await agent_module.execute_action(
            parsed=parsed,
            phone="573001234567",
            org_id=4242,
            table_context=MOCK_TABLE,
            session_state={"has_order": False, "order_delivered": False, "active": True},
        )

    assert len(saved_orders) == 2, f"Expected 2 orders (kitchen+bar), got {len(saved_orders)}"

    stations = {o["station"] for o in saved_orders}
    assert "kitchen" in stations, "There must be a kitchen order"
    assert "bar" in stations, "There must be a bar order"

    kitchen_order = next(o for o in saved_orders if o["station"] == "kitchen")
    bar_order     = next(o for o in saved_orders if o["station"] == "bar")

    # Kitchen: only "Pizza" (category="Comidas", not in bar_categories)
    kitchen_names = {i["name"] for i in kitchen_order["items"]}
    assert "Pizza" in kitchen_names
    assert "Mojito" not in kitchen_names

    # Bar: "Mojito" and "Agua" (category="Bebidas", in bar_categories)
    bar_names = {i["name"] for i in bar_order["items"]}
    assert "Mojito" in bar_names
    assert "Agua" in bar_names
    assert "Pizza" not in bar_names

    # Correct totals
    assert kitchen_order["total"] == 35000
    assert bar_order["total"] == 35000  # 25000 + 10000


@pytest.mark.asyncio
async def test_execute_action_without_bar_uses_station_all():
    """With bar_enabled=False the whole order goes to station='all' (kitchen, original behavior)."""
    saved_orders = []

    with (
        patch.object(agent_module.db, "db_get_cart", AsyncMock(return_value=MOCK_CART_MIXED)),
        patch.object(agent_module.orders, "get_cart_total", AsyncMock(return_value=70000)),
        patch.object(agent_module.db, "db_get_base_order_id", AsyncMock(return_value=None)),
        patch.object(agent_module.db, "db_get_restaurant_by_org_id", AsyncMock(return_value=MOCK_RESTAURANT_NO_BAR)),
        patch.object(agent_module.db, "db_save_table_order", AsyncMock(side_effect=lambda o: saved_orders.append(o))),
        patch.object(agent_module.db, "db_deduct_inventory_for_order", AsyncMock()),
        patch.object(agent_module.orders, "clear_cart", AsyncMock()),
        patch.object(agent_module.db, "db_session_mark_order", AsyncMock()),
        patch.object(agent_module.db, "get_pool", AsyncMock(return_value=MagicMock(
            acquire=MagicMock(return_value=MagicMock(
                __aenter__=AsyncMock(return_value=MagicMock(execute=AsyncMock())),
                __aexit__=AsyncMock(return_value=None),
            ))
        ))),
    ):
        parsed = {"action": "order", "items": [], "reply": "Pedido recibido"}
        await agent_module.execute_action(
            parsed=parsed,
            phone="573001234567",
            org_id=4242,
            table_context=MOCK_TABLE,
            session_state={"has_order": False, "order_delivered": False, "active": True},
        )

    assert len(saved_orders) == 1, "Without bar active it must create only ONE order"
    assert saved_orders[0]["station"] == "all"
    assert len(saved_orders[0]["items"]) == 3  # all items together


@pytest.mark.asyncio
async def test_execute_action_drinks_only_uses_bar():
    """All drinks (bar only): ONE order with station='bar' — it used to be
    'all', so a round of beers also showed up on the kitchen screen."""
    cart_drinks_only = {
        "items": [
            {"name": "Mojito",     "price": 25000, "quantity": 1, "subtotal": 25000, "category": "Bebidas"},
            {"name": "Cerveza IPA","price": 18000, "quantity": 2, "subtotal": 36000, "category": "Bebidas"},
        ]
    }
    saved_orders = []

    with (
        patch.object(agent_module.db, "db_get_cart", AsyncMock(return_value=cart_drinks_only)),
        patch.object(agent_module.orders, "get_cart_total", AsyncMock(return_value=61000)),
        patch.object(agent_module.db, "db_get_base_order_id", AsyncMock(return_value=None)),
        patch.object(agent_module.db, "db_get_restaurant_by_org_id", AsyncMock(return_value=MOCK_RESTAURANT_BAR)),
        patch.object(agent_module.db, "db_save_table_order", AsyncMock(side_effect=lambda o: saved_orders.append(o))),
        patch.object(agent_module.db, "db_deduct_inventory_for_order", AsyncMock()),
        patch.object(agent_module.orders, "clear_cart", AsyncMock()),
        patch.object(agent_module.db, "db_session_mark_order", AsyncMock()),
        patch.object(agent_module.db, "get_pool", AsyncMock(return_value=MagicMock(
            acquire=MagicMock(return_value=MagicMock(
                __aenter__=AsyncMock(return_value=MagicMock(execute=AsyncMock())),
                __aexit__=AsyncMock(return_value=None),
            ))
        ))),
    ):
        parsed = {"action": "order", "items": [], "reply": "Bebidas pedidas"}
        await agent_module.execute_action(
            parsed=parsed,
            phone="573001234567",
            org_id=4242,
            table_context=MOCK_TABLE,
            session_state={"has_order": False, "order_delivered": False, "active": True},
        )

    # Drinks only → kitchen_items empty → one ticket, and it belongs to the bar.
    assert len(saved_orders) == 1
    assert saved_orders[0]["station"] == "bar"
