"""
Suite — Tables & KDS flow (50 tests)
tests/test_tables_flow.py

Cubre:
  A.  Gestión de mesas (CRUD)                         [1–6]
  B.  POS manual order                                 [7–16]
  C.  KDS — get_table_orders (SQL raw via pool)        [17–24]
  D.  Cambio de status de orden (SQL raw via pool)     [25–30]
  E.  Split-checks y pago                              [31–42]
  F.  Alertas al mesero (waiter alerts)                [43–46]
  G.  db_get_base_order_id — fix bug duplicación       [47–50]
"""
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_pool, make_row, patch_auth
import app.services.database as db_mod

_HEADERS = {"Authorization": "Bearer tok"}


def _auth(monkeypatch, features=None):
    if features is None:
        features = {"staff_tips": True, "dian_active": False}
    r = patch_auth(monkeypatch, features=features)
    monkeypatch.setattr(db_mod, "db_check_module", AsyncMock(return_value=True))
    return r


def _mock_pool(monkeypatch, rows=None, fetchrow_result=None):
    """Patch db.get_pool() to return a mock connection yielding given rows."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.execute = AsyncMock()
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    return conn


# ─── shared fixtures ──────────────────────────────────────────────────────────

_TABLE = {
    "id": "TBL-001", "restaurant_id": 1, "name": "Mesa 1",
    "status": "libre", "capacity": 4, "branch_id": 1,
}

_ORDER_ROW = {
    "id": "MESA-AA3E4A", "table_id": "TBL-001", "table_name": "Mesa 1",
    "phone": "manual", "items": json.dumps([{"name": "Moñona", "quantity": 1, "price": 25000}]),
    "status": "recibido", "notes": "", "total": 25000, "base_order_id": "MESA-AA3E4A",
    "sub_number": 1, "station": "all", "branch_id": 1, "created_at": "2026-04-08T10:00:00",
}
_ORDER_ROW2 = {**_ORDER_ROW, "id": "MESA-AA3E4A-2", "sub_number": 2, "status": "en_preparacion"}

_CHECK = {
    "id": "chk-001", "base_order_id": "MESA-AA3E4A", "check_number": 1,
    "items": json.dumps([{"name": "Moñona", "qty": 1, "unit_price": 25000, "subtotal": 25000}]),
    "subtotal": 25000, "tax_amount": 0, "total": 25000,
    "status": "open", "tip_amount": 0, "proposed_payments": None, "proposed_tip": None,
}


# ══════════════════════════════════════════════════════════════════════════════
# A. GESTIÓN DE MESAS
# ══════════════════════════════════════════════════════════════════════════════

def test_get_tables_returns_list(client, monkeypatch):
    """GET /api/tables → 200, lista de mesas."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_tables", AsyncMock(return_value=[_TABLE]))
    r = client.get("/api/tables", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["tables"][0]["id"] == "TBL-001"


def test_get_tables_empty(client, monkeypatch):
    """GET /api/tables sin mesas → lista vacía."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_tables", AsyncMock(return_value=[]))
    r = client.get("/api/tables", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["tables"] == []


def test_create_table_success(client, monkeypatch):
    """POST /api/tables crea una mesa automáticamente."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_auto_create_table",
                        AsyncMock(return_value={"id": "TBL-NEW", "name": "Mesa 5"}))
    r = client.post("/api/tables", json={}, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["table_id"] == "TBL-NEW"


def test_create_table_returns_name(client, monkeypatch):
    """POST /api/tables retorna el nombre generado."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_auto_create_table",
                        AsyncMock(return_value={"id": "TBL-3", "name": "Mesa 3"}))
    r = client.post("/api/tables", json={}, headers=_HEADERS)
    assert r.json()["name"] == "Mesa 3"


def test_delete_table_success(client, monkeypatch):
    """DELETE /api/tables/{id} → 200."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_delete_table", AsyncMock())
    r = client.delete("/api/tables/TBL-001", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_delete_table_calls_db(client, monkeypatch):
    """DELETE /api/tables/{id} llama a db_delete_table con el id correcto."""
    _auth(monkeypatch)
    mock_del = AsyncMock()
    monkeypatch.setattr(db_mod, "db_delete_table", mock_del)
    client.delete("/api/tables/TBL-SPEC", headers=_HEADERS)
    mock_del.assert_awaited_once_with("TBL-SPEC")


# ══════════════════════════════════════════════════════════════════════════════
# B. POS MANUAL ORDER
# ══════════════════════════════════════════════════════════════════════════════

def _pos_body(**kwargs):
    base = {
        "table_id": "TBL-001", "table_name": "Mesa 1",
        "items": [{"name": "Moñona", "quantity": 1, "price": 25000, "subtotal": 25000}],
        "total": 25000, "notes": "", "station": "all", "branch_id": 1,
    }
    base.update(kwargs)
    return base


def test_pos_order_primera_orden(client, monkeypatch):
    """Primera orden de la mesa → sub_number=1, order_id con prefijo pos-."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True
    saved = db_mod.db_save_table_order.call_args[0][0]
    assert saved["sub_number"] == 1
    assert saved["id"].startswith("pos-")


def test_pos_order_sub_orden(client, monkeypatch):
    """Segunda orden en la misma mesa → sub_number=2, base_order_id heredado."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value="MESA-AA3E4A"))
    monkeypatch.setattr(db_mod, "db_get_next_sub_number", AsyncMock(return_value=2))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert r.status_code == 200
    saved = db_mod.db_save_table_order.call_args[0][0]
    assert saved["sub_number"] == 2
    assert saved["base_order_id"] == "MESA-AA3E4A"


def test_pos_order_station_kitchen(client, monkeypatch):
    """Station kitchen → mensaje indica cocina."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(station="kitchen"), headers=_HEADERS)
    assert r.status_code == 200
    assert "cocina" in r.json()["message"].lower()


def test_pos_order_station_bar(client, monkeypatch):
    """Station bar → mensaje indica bar."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(station="bar"), headers=_HEADERS)
    assert r.status_code == 200
    assert "bar" in r.json()["message"].lower()


def test_pos_order_station_all(client, monkeypatch):
    """Station all → mensaje indica cocina y bar."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(station="all"), headers=_HEADERS)
    assert r.status_code == 200
    msg = r.json()["message"].lower()
    assert "cocina" in msg or "bar" in msg


def test_pos_order_returns_order_id(client, monkeypatch):
    """Respuesta incluye order_id."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert "order_id" in r.json()


def test_pos_order_save_called_once(client, monkeypatch):
    """db_save_table_order se llama exactamente una vez (sin duplicación)."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    save_mock = AsyncMock()
    monkeypatch.setattr(db_mod, "db_save_table_order", save_mock)
    client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert save_mock.await_count == 1


def test_pos_order_missing_table_id(client, monkeypatch):
    """Sin table_id → 422."""
    _auth(monkeypatch)
    body = _pos_body()
    del body["table_id"]
    r = client.post("/api/pos/order", json=body, headers=_HEADERS)
    assert r.status_code == 422


def test_pos_order_branch_id_from_body(client, monkeypatch):
    """branch_id del body se usa si está presente."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(branch_id=5), headers=_HEADERS)
    assert r.status_code == 200
    saved = db_mod.db_save_table_order.call_args[0][0]
    assert saved["branch_id"] == 5


# ══════════════════════════════════════════════════════════════════════════════
# C. KDS — GET TABLE ORDERS (usa get_pool() raw)
# ══════════════════════════════════════════════════════════════════════════════

def test_get_table_orders_returns_orders(client, monkeypatch):
    """GET /api/table-orders → 200, lista de órdenes."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["orders"]) == 1


def test_get_table_orders_empty(client, monkeypatch):
    """Sin órdenes activas → lista vacía."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["orders"] == []


def test_get_table_orders_items_deserialized(client, monkeypatch):
    """Items JSON string se deserializa a lista en la respuesta."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    items = r.json()["orders"][0]["items"]
    assert isinstance(items, list)
    assert items[0]["name"] == "Moñona"


def test_get_table_orders_multiple_sub_orders(client, monkeypatch):
    """Múltiples sub-órdenes de la misma mesa aparecen separadas."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW), make_row(_ORDER_ROW2)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert len(r.json()["orders"]) == 2
    sub_nums = {o["sub_number"] for o in r.json()["orders"]}
    assert sub_nums == {1, 2}


def test_get_table_orders_station_filter(client, monkeypatch):
    """?station=bar → solo órdenes de bar."""
    _auth(monkeypatch)
    bar_row = {**_ORDER_ROW, "id": "bar-01", "station": "bar"}
    _mock_pool(monkeypatch, rows=[make_row(bar_row)])
    r = client.get("/api/table-orders?station=bar", headers=_HEADERS)
    assert r.status_code == 200
    # El filtro se aplica post-fetch; si hay solo uno y es bar, lo incluye
    for o in r.json()["orders"]:
        assert o["station"] in ("bar", "all")


def test_get_table_orders_unauthenticated(client, monkeypatch):
    """Sin auth → 401/403."""
    from unittest.mock import AsyncMock as _AM
    monkeypatch.setattr("app.routes.deps.verify_token", _AM(return_value=None))
    r = client.get("/api/table-orders")
    assert r.status_code in (401, 403)


def test_get_table_orders_status_field_present(client, monkeypatch):
    """Cada orden tiene campo status."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert "status" in r.json()["orders"][0]


def test_get_table_orders_branch_header(client, monkeypatch):
    """X-Branch-ID header se respeta sin error."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[])
    r = client.get("/api/table-orders", headers={**_HEADERS, "X-Branch-ID": "2"})
    assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# D. CAMBIO DE STATUS DE ORDEN (usa get_pool() raw)
# ══════════════════════════════════════════════════════════════════════════════

def _mock_pool_for_status(monkeypatch, order_row=None):
    """Mock pool que devuelve el order_row en fetchrow y ejecuta execute."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=make_row(order_row) if order_row else None)
    conn.execute = AsyncMock()
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    return conn


def test_update_order_status_en_preparacion(client, monkeypatch):
    """POST /api/table-orders/{id}/status → en_preparacion."""
    _auth(monkeypatch)
    order_rec = {"phone": "manual", "table_name": "Mesa 1",
                 "base_order_id": "MESA-AA3E4A", "table_id": "TBL-001"}
    _mock_pool_for_status(monkeypatch, order_rec)
    monkeypatch.setattr(db_mod, "db_update_table_order_status", AsyncMock())
    r = client.post("/api/table-orders/MESA-AA3E4A/status",
                    json={"status": "en_preparacion"}, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "en_preparacion"


def test_update_order_status_listo(client, monkeypatch):
    """POST status → listo."""
    _auth(monkeypatch)
    order_rec = {"phone": "manual", "table_name": "Mesa 1",
                 "base_order_id": "MESA-AA3E4A", "table_id": "TBL-001"}
    _mock_pool_for_status(monkeypatch, order_rec)
    monkeypatch.setattr(db_mod, "db_update_table_order_status", AsyncMock())
    r = client.post("/api/table-orders/MESA-AA3E4A/status",
                    json={"status": "listo"}, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "listo"


def test_update_order_status_invalid(client, monkeypatch):
    """Status inválido → 400."""
    _auth(monkeypatch)
    _mock_pool_for_status(monkeypatch)
    r = client.post("/api/table-orders/MESA-AA3E4A/status",
                    json={"status": "volando"}, headers=_HEADERS)
    assert r.status_code in (400, 422)


def test_update_order_status_not_found(client, monkeypatch):
    """Orden inexistente → 404."""
    _auth(monkeypatch)
    _mock_pool_for_status(monkeypatch, order_row=None)
    r = client.post("/api/table-orders/NOPE/status",
                    json={"status": "listo"}, headers=_HEADERS)
    assert r.status_code == 404


def test_update_order_status_cancelado(client, monkeypatch):
    """POST status → cancelado."""
    _auth(monkeypatch)
    order_rec = {"phone": "manual", "table_name": "Mesa 1",
                 "base_order_id": "MESA-AA3E4A", "table_id": "TBL-001"}
    _mock_pool_for_status(monkeypatch, order_rec)
    monkeypatch.setattr(db_mod, "db_update_table_order_status", AsyncMock())
    r = client.post("/api/table-orders/MESA-AA3E4A/status",
                    json={"status": "cancelado"}, headers=_HEADERS)
    assert r.status_code == 200


def test_update_order_status_returns_order_id(client, monkeypatch):
    """Respuesta incluye order_id."""
    _auth(monkeypatch)
    order_rec = {"phone": "manual", "table_name": "Mesa 1",
                 "base_order_id": "MESA-AA3E4A", "table_id": "TBL-001"}
    _mock_pool_for_status(monkeypatch, order_rec)
    monkeypatch.setattr(db_mod, "db_update_table_order_status", AsyncMock())
    r = client.post("/api/table-orders/MESA-AA3E4A/status",
                    json={"status": "listo"}, headers=_HEADERS)
    assert r.json()["order_id"] == "MESA-AA3E4A"


# ══════════════════════════════════════════════════════════════════════════════
# E. SPLIT-CHECKS Y PAGO
# ══════════════════════════════════════════════════════════════════════════════

_TICKET = {
    "base_order_id": "MESA-AA3E4A",
    "items": [{"name": "Moñona", "quantity": 1, "price": 25000}],
    "total": 25000,
}

def test_create_checks_success(client, monkeypatch):
    """POST /checks → 200, checks creados."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", AsyncMock(return_value=_TICKET))
    monkeypatch.setattr(db_mod, "db_create_checks", AsyncMock(return_value=[_CHECK]))
    body = {"checks": [{"check_number": 1, "items": [{"name": "Moñona", "qty": 1, "unit_price": 25000}]}]}
    r = client.post("/api/table-orders/MESA-AA3E4A/checks", json=body, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_create_checks_bill_not_found(client, monkeypatch):
    """Ticket inexistente → 404."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", AsyncMock(return_value=None))
    r = client.post("/api/table-orders/NOPE/checks",
                    json={"checks": []}, headers=_HEADERS)
    assert r.status_code == 404


def test_create_checks_qty_exceeded(client, monkeypatch):
    """Check con más qty que la disponible → 400."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", AsyncMock(return_value=_TICKET))
    body = {"checks": [{"check_number": 1, "items": [{"name": "Moñona", "qty": 5, "unit_price": 25000}]}]}
    r = client.post("/api/table-orders/MESA-AA3E4A/checks", json=body, headers=_HEADERS)
    assert r.status_code == 400


def test_get_checks_returns_list(client, monkeypatch):
    """GET /checks → lista de checks."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_checks", AsyncMock(return_value=[_CHECK]))
    r = client.get("/api/table-orders/MESA-AA3E4A/checks", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["checks"]) == 1


def test_get_checks_empty(client, monkeypatch):
    """Sin checks → lista vacía."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_checks", AsyncMock(return_value=[]))
    r = client.get("/api/table-orders/MESA-AA3E4A/checks", headers=_HEADERS)
    assert r.json()["checks"] == []


def test_pay_check_not_found(client, monkeypatch):
    """Check inexistente → 404."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=None))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/NOPE/pay",
                    json={"payments": [{"method": "efectivo", "amount": 25000}], "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 404


def test_pay_check_wrong_base_order(client, monkeypatch):
    """Check de otra mesa → 400."""
    _auth(monkeypatch)
    wrong_check = {**_CHECK, "base_order_id": "OTHER"}
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=wrong_check))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 25000}], "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 400


def test_pay_check_already_paid(client, monkeypatch):
    """Check ya pagado → 400."""
    _auth(monkeypatch)
    paid = {**_CHECK, "status": "paid"}
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=paid))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 25000}], "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 400


def test_pay_check_tip_exceeds_50pct(client, monkeypatch):
    """Propina > 50% del total → 400."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=_CHECK))
    import app.services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "get_billing_config", AsyncMock(return_value={}))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 50000}],
                          "tip_amount": 20000},  # >50% de 25000
                    headers=_HEADERS)
    assert r.status_code == 400


def test_pay_check_insufficient_payment(client, monkeypatch):
    """Pago insuficiente → 400."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=_CHECK))
    import app.services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "get_billing_config", AsyncMock(return_value={}))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 1000}],
                          "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 400


def test_delete_check_success(client, monkeypatch):
    """DELETE /checks/{id} → 200."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_delete_open_check", AsyncMock(return_value=True))
    r = client.delete("/api/table-orders/MESA-AA3E4A/checks/chk-001", headers=_HEADERS)
    assert r.status_code == 200


def test_delete_check_not_found(client, monkeypatch):
    """DELETE check inexistente o ya procesado → 400."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_delete_open_check", AsyncMock(return_value=False))
    r = client.delete("/api/table-orders/MESA-AA3E4A/checks/NOPE", headers=_HEADERS)
    assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# F. ALERTAS AL MESERO
# ══════════════════════════════════════════════════════════════════════════════

def test_create_waiter_alert_success(client, monkeypatch):
    """POST /api/waiter-alerts/admin-call → 200."""
    _auth(monkeypatch)
    alert = {"id": 1, "type": "admin_call", "status": "pending"}
    monkeypatch.setattr(db_mod, "db_create_waiter_alert", AsyncMock(return_value=alert))
    r = client.post("/api/waiter-alerts/admin-call",
                    json={"phone": "", "table_id": "", "table_name": "", "bot_number": ""},
                    headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_dismiss_waiter_alert_success(client, monkeypatch):
    """POST /api/waiter-alerts/{id}/dismiss → 200."""
    _auth(monkeypatch)
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    r = client.post("/api/waiter-alerts/1/dismiss", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_dismiss_alert_calls_delete(client, monkeypatch):
    """dismiss ejecuta DELETE en la DB con el ID correcto."""
    _auth(monkeypatch)
    conn = MagicMock()
    executed = []
    async def capture(q, *args):
        executed.append((q, args))
    conn.execute = capture
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    client.post("/api/waiter-alerts/42/dismiss", headers=_HEADERS)
    assert any("DELETE" in q for q, _ in executed)
    assert any(42 in args for _, args in executed)


def test_get_waiter_alerts_no_auth(client, monkeypatch):
    """GET /api/waiter-alerts sin auth → 401/403."""
    from unittest.mock import AsyncMock as _AM
    monkeypatch.setattr("app.routes.deps.verify_token", _AM(return_value=None))
    r = client.get("/api/waiter-alerts")
    assert r.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════════════════════
# G. BUG DUPLICACIÓN — db_get_base_order_id
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_base_order_id_sin_sesion_activa_retorna_none(monkeypatch):
    """
    FIX DUPLICACIÓN: Sin sesión activa para la mesa → None.
    Evita que nueva sesión añada "Adicional #N" a una sesión anterior.
    """
    from app.repositories.tables_repo import db_get_base_order_id

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        None,  # no hay sesión activa
        make_row({"base_id": "MESA-OLD"}),  # no debe llegar aquí
    ])
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.tables_repo._get_pool", AsyncMock(return_value=pool))

    result = await db_get_base_order_id("TBL-001")
    assert result is None
    assert conn.fetchrow.call_count == 1  # solo 1 query: la de sesión


@pytest.mark.asyncio
async def test_base_order_id_con_sesion_activa_retorna_id(monkeypatch):
    """Con sesión activa → retorna el base_order_id de la orden existente."""
    from app.repositories.tables_repo import db_get_base_order_id

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        make_row({"id": "sess-001"}),
        make_row({"base_id": "MESA-AA3E4A"}),
    ])
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.tables_repo._get_pool", AsyncMock(return_value=pool))

    result = await db_get_base_order_id("TBL-001")
    assert result == "MESA-AA3E4A"


@pytest.mark.asyncio
async def test_base_order_id_sesion_activa_sin_ordenes_retorna_none(monkeypatch):
    """Sesión activa pero sin órdenes previas → None (primera orden del cliente)."""
    from app.repositories.tables_repo import db_get_base_order_id

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        make_row({"id": "sess-001"}),
        None,
    ])
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.tables_repo._get_pool", AsyncMock(return_value=pool))

    result = await db_get_base_order_id("TBL-001")
    assert result is None


@pytest.mark.asyncio
async def test_base_order_id_sesion_cerrada_no_reutiliza(monkeypatch):
    """Mesa con sesión cerrada (status != active) → None, no reutiliza órdenes."""
    from app.repositories.tables_repo import db_get_base_order_id

    conn = MagicMock()
    # Ninguna sesión activa (query filtra status='active')
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        make_row({"base_id": "MESA-OLD"}),
    ])
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.tables_repo._get_pool", AsyncMock(return_value=pool))

    result = await db_get_base_order_id("TBL-001")
    assert result is None
