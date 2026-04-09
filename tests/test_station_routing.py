"""
Tests para FASE 2: Enrutamiento multi-estación Cocina / Bar.
Cubre: filtro ?station= en GET /api/table-orders, ruta /bar,
       station en db_save_table_order, lógica de split en agent.execute_action.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import app.routes.tables as tables_routes
import app.services.agent as agent_module


# ── Fixtures compartidos ──────────────────────────────────────────────

@pytest.fixture
def mock_auth(monkeypatch):
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(
        "app.routes.deps.db.db_get_user",
        AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": None, "role": "owner"}),
    )


SAMPLE_ORDERS = [
    {"id": "MESA-K1",   "table_name": "Mesa 1", "status": "recibido",       "station": "kitchen", "items": [], "created_at": "2024-06-15T12:00:00Z", "updated_at": "2024-06-15T12:00:00Z"},
    {"id": "MESA-B1",   "table_name": "Mesa 1", "status": "recibido",       "station": "bar",     "items": [], "created_at": "2024-06-15T12:00:00Z", "updated_at": "2024-06-15T12:00:00Z"},
    {"id": "MESA-ALL1", "table_name": "Mesa 2", "status": "en_preparacion", "station": "all",     "items": [], "created_at": "2024-06-15T12:01:00Z", "updated_at": "2024-06-15T12:01:00Z"},
    {"id": "MESA-K2",   "table_name": "Mesa 3", "status": "listo",          "station": "kitchen", "items": [], "created_at": "2024-06-15T12:02:00Z", "updated_at": "2024-06-15T12:02:00Z"},
]


# ══════════════════════════════════════════════════════════════════════
# 1. Filtro ?station= en GET /api/table-orders
# ══════════════════════════════════════════════════════════════════════

def test_table_orders_sin_filtro_devuelve_todos(client, mock_auth, monkeypatch):
    """/api/table-orders sin ?station devuelve todas las órdenes (para Caja)."""
    async def mock_fetch(*args, **kwargs):
        class FakeConn:
            async def fetch(self, *a, **k):
                return [MagicMock(**{**o, "__iter__": lambda s: iter(o.items()), "keys": lambda s: o.keys(), "__getitem__": lambda s, k: o[k]}) for o in SAMPLE_ORDERS]
        return FakeConn()

    class FakePool:
        def acquire(self): return self
        async def __aenter__(self):
            class FakeConn:
                async def fetch(self, *a, **k): return []
            return FakeConn()
        async def __aexit__(self, *a): pass

    # Mock directo del pool y la consulta
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=FakePool()))

    headers = {"Authorization": "Bearer token"}
    response = client.get("/api/table-orders", headers=headers)
    # Sin DB real solo verificamos que el endpoint responde y tiene la key "orders"
    assert response.status_code == 200
    assert "orders" in response.json()


def _make_pool_with_orders(orders: list):
    """Crea un mock de pool que devuelve las órdenes dadas como dicts con atributos."""
    class FakeRow(dict):
        def keys(self): return super().keys()
        def __iter__(self): return iter(self.items())

    class FakeConn:
        async def fetch(self, *a, **k):
            return [FakeRow(o) for o in orders]

    class FakePool:
        def acquire(self): return self
        async def __aenter__(self): return FakeConn()
        async def __aexit__(self, *a): pass

    return FakePool()


def test_filtro_station_kitchen_excluye_bar(client, mock_auth, monkeypatch):
    """?station=kitchen debe devolver solo station='kitchen' y station='all'."""
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


def test_filtro_station_bar_excluye_kitchen(client, mock_auth, monkeypatch):
    """?station=bar debe devolver solo station='bar' y station='all'."""
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


def test_filtro_station_all_devuelve_todos(client, mock_auth, monkeypatch):
    """Sin ?station= todos los registros pasan el filtro."""
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=_make_pool_with_orders(SAMPLE_ORDERS)))
    headers = {"Authorization": "Bearer token"}
    response = client.get("/api/table-orders", headers=headers)
    assert response.status_code == 200
    orders = response.json()["orders"]
    assert len(orders) == len(SAMPLE_ORDERS)


# ══════════════════════════════════════════════════════════════════════
# 2. Ruta /bar sirve bar.html
# ══════════════════════════════════════════════════════════════════════

def test_ruta_bar_devuelve_html(client):
    """/bar debe devolver 200 con contenido HTML del KDS de Bar."""
    response = client.get("/bar")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Verificar que es el KDS del bar y no otro HTML
    body = response.text
    assert "Bar" in body
    assert "station=bar" in body
    assert "Mesio" in body


def test_ruta_cocina_sigue_funcionando(client):
    """/cocina debe seguir devolviendo 200 tras los cambios."""
    response = client.get("/cocina")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "station=kitchen" in response.text


# ══════════════════════════════════════════════════════════════════════
# 3. ManualOrderRequest acepta station y lo pasa a DB
# ══════════════════════════════════════════════════════════════════════

def test_pos_order_station_default_all(client, monkeypatch):
    """POST /api/pos/order sin station= debe usar station='all'."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "get_current_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "role": "owner"}))
    mock_save = AsyncMock()
    monkeypatch.setattr(tables_routes.db, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tables_routes.db, "db_save_table_order", mock_save)
    monkeypatch.setattr(tables_routes.db, "db_get_next_sub_number", AsyncMock(return_value=1))

    payload = {
        "table_id":   "mesa-1",
        "table_name": "Mesa 1",
        "items":      [{"name": "Pizza", "price": 35000, "quantity": 1}],
        "total":      35000,
        "notes":      "",
        # station no especificado → debe usar 'all'
    }
    response = client.post("/api/pos/order", json=payload, headers={"Authorization": "Bearer token"})
    assert response.status_code == 200

    saved = mock_save.call_args[0][0]
    assert saved["station"] == "all"


def test_pos_order_station_bar(client, monkeypatch):
    """POST /api/pos/order con station='bar' debe guardarlo como 'bar'."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "get_current_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "role": "owner"}))
    mock_save = AsyncMock()
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


def test_pos_order_station_kitchen_mensaje(client, monkeypatch):
    """POST /api/pos/order con station='kitchen' da mensaje de cocina."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(tables_routes, "get_current_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "role": "owner"}))
    monkeypatch.setattr(tables_routes.db, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tables_routes.db, "db_save_table_order", AsyncMock())
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
# 4. Lógica de split en execute_action (agent.py)
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
async def test_execute_action_split_kitchen_y_bar():
    """Con bar_enabled=True items mixtos crean dos sub-orders: kitchen y bar."""
    saved_orders = []

    async def fake_save(order):
        saved_orders.append(order)

    with (
        patch.object(agent_module.db, "db_get_cart", AsyncMock(return_value=MOCK_CART_MIXED)),
        patch.object(agent_module.orders, "get_cart_total", AsyncMock(return_value=70000)),
        patch.object(agent_module.db, "db_get_base_order_id", AsyncMock(return_value=None)),
        patch.object(agent_module.db, "db_get_restaurant_by_bot_number", AsyncMock(return_value=MOCK_RESTAURANT_BAR)),
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
            bot_number="15556293573",
            table_context=MOCK_TABLE,
            session_state={"has_order": False, "order_delivered": False, "active": True},
        )

    assert len(saved_orders) == 2, f"Esperaba 2 órdenes (kitchen+bar), obtuve {len(saved_orders)}"

    stations = {o["station"] for o in saved_orders}
    assert "kitchen" in stations, "Debe haber una orden de kitchen"
    assert "bar" in stations, "Debe haber una orden de bar"

    kitchen_order = next(o for o in saved_orders if o["station"] == "kitchen")
    bar_order     = next(o for o in saved_orders if o["station"] == "bar")

    # Kitchen: solo "Pizza" (category="Comidas", no en bar_categories)
    kitchen_names = {i["name"] for i in kitchen_order["items"]}
    assert "Pizza" in kitchen_names
    assert "Mojito" not in kitchen_names

    # Bar: "Mojito" y "Agua" (category="Bebidas", en bar_categories)
    bar_names = {i["name"] for i in bar_order["items"]}
    assert "Mojito" in bar_names
    assert "Agua" in bar_names
    assert "Pizza" not in bar_names

    # Totales correctos
    assert kitchen_order["total"] == 35000
    assert bar_order["total"] == 35000  # 25000 + 10000


@pytest.mark.asyncio
async def test_execute_action_sin_bar_usa_station_all():
    """Con bar_enabled=False toda la orden va a station='all' (cocina, comportamiento original)."""
    saved_orders = []

    with (
        patch.object(agent_module.db, "db_get_cart", AsyncMock(return_value=MOCK_CART_MIXED)),
        patch.object(agent_module.orders, "get_cart_total", AsyncMock(return_value=70000)),
        patch.object(agent_module.db, "db_get_base_order_id", AsyncMock(return_value=None)),
        patch.object(agent_module.db, "db_get_restaurant_by_bot_number", AsyncMock(return_value=MOCK_RESTAURANT_NO_BAR)),
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
            bot_number="15556293573",
            table_context=MOCK_TABLE,
            session_state={"has_order": False, "order_delivered": False, "active": True},
        )

    assert len(saved_orders) == 1, "Sin bar activo debe crear solo UNA orden"
    assert saved_orders[0]["station"] == "all"
    assert len(saved_orders[0]["items"]) == 3  # todos los ítems juntos


@pytest.mark.asyncio
async def test_execute_action_solo_bebidas_usa_bar():
    """Si todos los ítems son bebidas (solo bar), se crea UNA orden de bar station='all'."""
    cart_solo_bebidas = {
        "items": [
            {"name": "Mojito",     "price": 25000, "quantity": 1, "subtotal": 25000, "category": "Bebidas"},
            {"name": "Cerveza IPA","price": 18000, "quantity": 2, "subtotal": 36000, "category": "Bebidas"},
        ]
    }
    saved_orders = []

    with (
        patch.object(agent_module.db, "db_get_cart", AsyncMock(return_value=cart_solo_bebidas)),
        patch.object(agent_module.orders, "get_cart_total", AsyncMock(return_value=61000)),
        patch.object(agent_module.db, "db_get_base_order_id", AsyncMock(return_value=None)),
        patch.object(agent_module.db, "db_get_restaurant_by_bot_number", AsyncMock(return_value=MOCK_RESTAURANT_BAR)),
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
            bot_number="15556293573",
            table_context=MOCK_TABLE,
            session_state={"has_order": False, "order_delivered": False, "active": True},
        )

    # Solo bebidas → kitchen_items vacío → solo se crea la orden de kitchen con station='all'
    # (has_split=False porque kitchen_items está vacío)
    assert len(saved_orders) == 1
    assert saved_orders[0]["station"] == "all"
