"""
Suite — Tables & KDS flow (50 tests)
tests/test_tables_flow.py

Covers:
  A.  Table management (CRUD)                          [1–6]
  B.  POS manual order                                 [7–16]
  C.  KDS — get_table_orders (SQL raw via pool)        [17–24]
  D.  Order status change (SQL raw via pool)           [25–30]
  E.  Split-checks and payment                          [31–42]
  F.  Waiter alerts                                     [43–46]
  G.  db_get_base_order_id — duplication bug fix        [47–50]
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
    # POS orders read the table (inside the caller's org) to learn its sede.
    # Check endpoints first prove the order is the caller's (table_checks has no RLS).
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_orders_by_base_id",
                        AsyncMock(side_effect=lambda oid, sede=None: [{"id": oid, "org_id": 1, "location_id": 1}]))
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_by_id", AsyncMock(side_effect=lambda tid: {"id": tid, "org_id": 1, "location_id": 1}))
    return r


def _mock_pool(monkeypatch, rows=None, fetchrow_result=None):
    """Patch db.get_pool() to return a mock connection yielding given rows."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetchval = AsyncMock(return_value=True)
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
    """GET /api/tables → 200, list of tables."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_tables", AsyncMock(return_value=[_TABLE]))
    r = client.get("/api/tables", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["tables"][0]["id"] == "TBL-001"


def test_get_tables_empty(client, monkeypatch):
    """GET /api/tables without tables → empty list."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_tables", AsyncMock(return_value=[]))
    r = client.get("/api/tables", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["tables"] == []


def test_create_table_success(client, monkeypatch):
    """POST /api/tables automatically creates a table."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_auto_create_table",
                        AsyncMock(return_value={"id": "TBL-NEW", "name": "Mesa 5"}))
    r = client.post("/api/tables", json={}, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["table_id"] == "TBL-NEW"


def test_create_table_returns_name(client, monkeypatch):
    """POST /api/tables returns the generated name."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_auto_create_table",
                        AsyncMock(return_value={"id": "TBL-3", "name": "Mesa 3"}))
    r = client.post("/api/tables", json={}, headers=_HEADERS)
    assert r.json()["name"] == "Mesa 3"


def test_delete_table_success(client, monkeypatch):
    """DELETE /api/tables/{id} → 200."""
    _auth(monkeypatch)
    conn = _mock_pool(monkeypatch, fetchrow_result=make_row({"branch_id": None}))
    monkeypatch.setattr(db_mod, "db_delete_table", AsyncMock())
    r = client.delete("/api/tables/TBL-001", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_delete_table_calls_db(client, monkeypatch):
    """DELETE /api/tables/{id} calls db_delete_table with the correct id."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, fetchrow_result=make_row({"branch_id": None}))
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


def test_pos_order_first_order(client, monkeypatch):
    """First order for the table → sub_number=1, order_id with pos- prefix."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True
    saved = db_mod.db_save_table_order.call_args[0][0]
    assert saved["sub_number"] == 1
    assert saved["id"].startswith("pos-")


def test_pos_order_sub_order(client, monkeypatch):
    """Second order on the same table → sub_number=2, inherited base_order_id."""
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
    """Station kitchen → message indicates kitchen."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(station="kitchen"), headers=_HEADERS)
    assert r.status_code == 200
    assert "cocina" in r.json()["message"].lower()


def test_pos_order_station_bar(client, monkeypatch):
    """Station bar → message indicates bar."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(station="bar"), headers=_HEADERS)
    assert r.status_code == 200
    assert "bar" in r.json()["message"].lower()


def test_pos_order_station_all(client, monkeypatch):
    """Station all → message indicates kitchen and bar."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(station="all"), headers=_HEADERS)
    assert r.status_code == 200
    msg = r.json()["message"].lower()
    assert "cocina" in msg or "bar" in msg


def test_pos_order_returns_order_id(client, monkeypatch):
    """Response includes order_id."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert "order_id" in r.json()


def test_pos_order_save_called_once(client, monkeypatch):
    """db_save_table_order is called exactly once (no duplication)."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    save_mock = AsyncMock()
    monkeypatch.setattr(db_mod, "db_save_table_order", save_mock)
    client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert save_mock.await_count == 1


def test_pos_order_missing_table_id(client, monkeypatch):
    """Without table_id → 422."""
    _auth(monkeypatch)
    body = _pos_body()
    del body["table_id"]
    r = client.post("/api/pos/order", json=body, headers=_HEADERS)
    assert r.status_code == 422


def test_pos_order_sede_comes_from_the_table_not_the_body(client, monkeypatch):
    """A body branch_id is ignored: the comanda goes to the table's own sede."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_base_order_id", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_save_table_order", AsyncMock())
    r = client.post("/api/pos/order", json=_pos_body(branch_id=5), headers=_HEADERS)
    assert r.status_code == 200
    saved = db_mod.db_save_table_order.call_args[0][0]
    assert saved["branch_id"] == 1
    assert saved["org_id"] == 1


def test_pos_order_rejects_another_orgs_table(client, monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_by_id",
                        AsyncMock(return_value={"id": "TBL-001", "org_id": 99, "location_id": 7}))
    save = AsyncMock()
    monkeypatch.setattr(db_mod, "db_save_table_order", save)
    r = client.post("/api/pos/order", json=_pos_body(), headers=_HEADERS)
    assert r.status_code == 404
    save.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# C. KDS — GET TABLE ORDERS (usa get_pool() raw)
# ══════════════════════════════════════════════════════════════════════════════

def test_get_table_orders_returns_orders(client, monkeypatch):
    """GET /api/table-orders → 200, list of orders."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["orders"]) == 1


def test_get_table_orders_empty(client, monkeypatch):
    """Without active orders → empty list."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["orders"] == []


def test_get_table_orders_items_deserialized(client, monkeypatch):
    """Items JSON string is deserialized into a list in the response."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    items = r.json()["orders"][0]["items"]
    assert isinstance(items, list)
    assert items[0]["name"] == "Moñona"


def test_get_table_orders_multiple_sub_orders(client, monkeypatch):
    """Multiple sub-orders for the same table appear separately."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW), make_row(_ORDER_ROW2)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert len(r.json()["orders"]) == 2
    sub_nums = {o["sub_number"] for o in r.json()["orders"]}
    assert sub_nums == {1, 2}


def test_get_table_orders_station_filter(client, monkeypatch):
    """?station=bar → only bar orders."""
    _auth(monkeypatch)
    bar_row = {**_ORDER_ROW, "id": "bar-01", "station": "bar"}
    _mock_pool(monkeypatch, rows=[make_row(bar_row)])
    r = client.get("/api/table-orders?station=bar", headers=_HEADERS)
    assert r.status_code == 200
    # El filtro se aplica post-fetch; si hay solo uno y es bar, lo incluye
    for o in r.json()["orders"]:
        assert o["station"] in ("bar", "all")


def test_get_table_orders_unauthenticated(client, monkeypatch):
    """Without auth → 401/403."""
    from unittest.mock import AsyncMock as _AM
    monkeypatch.setattr("app.routes.deps.verify_token", _AM(return_value=None))
    r = client.get("/api/table-orders")
    assert r.status_code in (401, 403)


def test_get_table_orders_status_field_present(client, monkeypatch):
    """Each order has a status field."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row(_ORDER_ROW)])
    r = client.get("/api/table-orders", headers=_HEADERS)
    assert "status" in r.json()["orders"][0]


def test_get_table_orders_branch_header(client, monkeypatch):
    """X-Branch-ID header is respected without error."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[])
    r = client.get("/api/table-orders", headers={**_HEADERS, "X-Branch-ID": "2"})
    assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# D. CAMBIO DE STATUS DE ORDEN (usa get_pool() raw)
# ══════════════════════════════════════════════════════════════════════════════

def _mock_pool_for_status(monkeypatch, order_row=None):
    """Mock pool that returns order_row in fetchrow and runs execute.
    The order belongs to the caller's org/sede (1) unless the row says otherwise."""
    if order_row:
        order_row = {"org_id": 1, "location_id": 1, **order_row}
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)  # tenant_connection's set_config
    conn.fetchrow = AsyncMock(return_value=make_row(order_row) if order_row else None)
    conn.execute = AsyncMock()
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    return conn


def test_update_order_status_in_preparation(client, monkeypatch):
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


def test_update_order_status_ready(client, monkeypatch):
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
    """Invalid status → 400."""
    _auth(monkeypatch)
    _mock_pool_for_status(monkeypatch)
    r = client.post("/api/table-orders/MESA-AA3E4A/status",
                    json={"status": "volando"}, headers=_HEADERS)
    assert r.status_code in (400, 422)


def test_update_order_status_not_found(client, monkeypatch):
    """Nonexistent order → 404."""
    _auth(monkeypatch)
    _mock_pool_for_status(monkeypatch, order_row=None)
    r = client.post("/api/table-orders/NOPE/status",
                    json={"status": "listo"}, headers=_HEADERS)
    assert r.status_code == 404


def test_update_order_status_cancelled(client, monkeypatch):
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
    """Response includes order_id."""
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
    "org_id": 1,
}

def test_create_checks_success(client, monkeypatch):
    """POST /checks → 200, checks created."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", AsyncMock(return_value=_TICKET))
    monkeypatch.setattr(db_mod, "db_create_checks", AsyncMock(return_value=[_CHECK]))
    body = {"checks": [{"check_number": 1, "items": [{"name": "Moñona", "qty": 1, "unit_price": 25000}]}]}
    r = client.post("/api/table-orders/MESA-AA3E4A/checks", json=body, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_create_checks_bill_not_found(client, monkeypatch):
    """Nonexistent ticket → 404."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", AsyncMock(return_value=None))
    r = client.post("/api/table-orders/NOPE/checks",
                    json={"checks": []}, headers=_HEADERS)
    assert r.status_code == 404


def test_create_checks_qty_exceeded(client, monkeypatch):
    """Check with more qty than available → 400."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", AsyncMock(return_value=_TICKET))
    body = {"checks": [{"check_number": 1, "items": [{"name": "Moñona", "qty": 5, "unit_price": 25000}]}]}
    r = client.post("/api/table-orders/MESA-AA3E4A/checks", json=body, headers=_HEADERS)
    assert r.status_code == 400


def test_get_checks_returns_list(client, monkeypatch):
    """GET /checks → list of checks."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_checks", AsyncMock(return_value=[_CHECK]))
    r = client.get("/api/table-orders/MESA-AA3E4A/checks", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["checks"]) == 1


def test_get_checks_of_another_orgs_order_is_404(client, monkeypatch):
    """The order is not the caller's (RLS returns no row): no checks leak."""
    _auth(monkeypatch)
    monkeypatch.setattr("app.routes.tables.tr.db_get_table_orders_by_base_id", AsyncMock(return_value=[]))
    checks = AsyncMock(return_value=[_CHECK])
    monkeypatch.setattr(db_mod, "db_get_checks", checks)
    r = client.get("/api/table-orders/MESA-AJENA/checks", headers=_HEADERS)
    assert r.status_code == 404
    checks.assert_not_called()


def test_update_order_status_of_another_org_is_404(client, monkeypatch):
    _auth(monkeypatch)
    _mock_pool_for_status(monkeypatch, {"phone": "manual", "table_name": "Mesa 9",
                                        "base_order_id": "X", "table_id": "T", "org_id": 99})
    upd = AsyncMock()
    monkeypatch.setattr(db_mod, "db_update_table_order_status", upd)
    r = client.post("/api/table-orders/X/status", json={"status": "listo"}, headers=_HEADERS)
    assert r.status_code == 404
    upd.assert_not_called()


def test_get_checks_empty(client, monkeypatch):
    """Without checks → empty list."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_checks", AsyncMock(return_value=[]))
    r = client.get("/api/table-orders/MESA-AA3E4A/checks", headers=_HEADERS)
    assert r.json()["checks"] == []


def test_pay_check_not_found(client, monkeypatch):
    """Nonexistent check → 404. New flow: claim returns None, then db_get_check
    returns None → 404."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_release_check", AsyncMock(return_value=False))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/NOPE/pay",
                    json={"payments": [{"method": "efectivo", "amount": 25000}], "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 404


def test_pay_check_wrong_base_order(client, monkeypatch):
    """Check from another table → 400. New flow: claim refuses (base_order mismatches),
    fallback db_get_check shows the wrong base_order, route returns 400."""
    _auth(monkeypatch)
    wrong_check = {**_CHECK, "base_order_id": "OTHER"}
    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=wrong_check))
    monkeypatch.setattr(db_mod, "db_release_check", AsyncMock(return_value=False))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 25000}], "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 400


def test_pay_check_already_paid(client, monkeypatch):
    """Check already processed → 409 (Conflict). Was 400 in the old flow.

    Race-free flow uses 409 because the request conflicts with the resource's
    current state — accurate HTTP semantic.
    """
    _auth(monkeypatch)
    paid = {**_CHECK, "status": "paid"}
    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", AsyncMock(return_value=None))
    monkeypatch.setattr(db_mod, "db_get_check", AsyncMock(return_value=paid))
    monkeypatch.setattr(db_mod, "db_release_check", AsyncMock(return_value=False))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 25000}], "tip_amount": 0},
                    headers=_HEADERS)
    assert r.status_code == 409


def test_pay_check_tip_exceeds_50pct(client, monkeypatch):
    """Tip > 50% of the total → 400. The check IS claimable (open → paying),
    but business validation rejects the tip."""
    _auth(monkeypatch)
    claimed_check = {**_CHECK, "status": "paying"}
    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", AsyncMock(return_value=claimed_check))
    monkeypatch.setattr(db_mod, "db_release_check", AsyncMock(return_value=True))
    import app.services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "get_billing_config", AsyncMock(return_value={}))
    r = client.post("/api/table-orders/MESA-AA3E4A/checks/chk-001/pay",
                    json={"payments": [{"method": "efectivo", "amount": 50000}],
                          "tip_amount": 20000},  # >50% de 25000
                    headers=_HEADERS)
    assert r.status_code == 400


def test_pay_check_insufficient_payment(client, monkeypatch):
    """Insufficient payment → 400. Claim succeeds, payment validation fails."""
    _auth(monkeypatch)
    claimed_check = {**_CHECK, "status": "paying"}
    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", AsyncMock(return_value=claimed_check))
    monkeypatch.setattr(db_mod, "db_release_check", AsyncMock(return_value=True))
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
    monkeypatch.setattr(db_mod, "db_get_checks", AsyncMock(return_value=[_CHECK]))  # chk-001 is this order's
    monkeypatch.setattr(db_mod, "db_delete_open_check", AsyncMock(return_value=True))
    r = client.delete("/api/table-orders/MESA-AA3E4A/checks/chk-001", headers=_HEADERS)
    assert r.status_code == 200


def test_delete_check_of_another_order_is_refused(client, monkeypatch):
    """A check id that is not one of this order's checks is never deleted."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_checks", AsyncMock(return_value=[_CHECK]))
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(db_mod, "db_delete_open_check", delete)
    r = client.delete("/api/table-orders/MESA-AA3E4A/checks/chk-ajeno", headers=_HEADERS)
    assert r.status_code == 400
    delete.assert_not_called()


def test_delete_check_not_found(client, monkeypatch):
    """DELETE nonexistent or already-processed check → 400."""
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
                    json={"phone": "", "table_id": "", "table_name": ""},
                    headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_dismiss_waiter_alert_success(client, monkeypatch):
    """POST /api/waiter-alerts/{id}/dismiss → 200.

    conn.execute must return the real asyncpg status string ("UPDATE 1") —
    db_dismiss_waiter_alert checks `result == "UPDATE 1"` to know whether the
    row actually matched (see the 2026-09 IDOR fix: dismiss now runs inside
    the caller's own tenant_scope(), so a foreign/unknown id legitimately
    matches zero rows and must 404 rather than silently reporting success).
    """
    _auth(monkeypatch)
    conn = _mock_pool(monkeypatch)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    r = client.post("/api/waiter-alerts/1/dismiss", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_dismiss_alert_sets_dismissed_flag_not_delete(client, monkeypatch):
    """dismiss must UPDATE waiter_alerts.dismissed=TRUE, never DELETE the row.

    BUG FOUND 2026-09 (security audit pass): app/repositories/tables_repo.py
    used to define db_dismiss_waiter_alert TWICE — a real soft-dismiss
    (UPDATE ... SET dismissed=TRUE) and, much later in the same file, a
    same-named DELETE FROM waiter_alerts. Python keeps only the last
    definition of a module-level name, so the DELETE version silently
    shadowed the real one and every dismiss call — including this test,
    which used to assert the DELETE happened — was permanently destroying
    alert rows instead of just marking them dismissed. Fixed by renaming the
    duplicate to db_delete_waiter_alert; this test now asserts the CORRECT
    (UPDATE, not DELETE) behaviour.
    """
    _auth(monkeypatch)
    conn = _mock_pool(monkeypatch)
    executed = []
    async def capture(q, *args):
        executed.append((q, args))
        return "UPDATE 1"
    conn.execute = capture
    r = client.post("/api/waiter-alerts/42/dismiss", headers=_HEADERS)
    assert r.status_code == 200
    assert any("UPDATE" in q and "dismissed" in q.lower() for q, _ in executed)
    assert not any("DELETE" in q for q, _ in executed)
    assert any(42 in args for _, args in executed)


def test_get_waiter_alerts_no_auth(client, monkeypatch):
    """GET /api/waiter-alerts without auth → 401/403."""
    from unittest.mock import AsyncMock as _AM
    monkeypatch.setattr("app.routes.deps.verify_token", _AM(return_value=None))
    r = client.get("/api/waiter-alerts")
    assert r.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════════════════════
# G. BUG DUPLICACIÓN — db_get_base_order_id
# Updated for tenant_connection() migration: patch app.services.database.get_pool
# and wrap calls in tenant_scope() (following test_customer_memory.py pattern).
# ══════════════════════════════════════════════════════════════════════════════

from unittest.mock import patch as _patch
from app.services.tenant_context import tenant_scope as _tenant_scope


def _make_tenant_conn(fetchrow_side_effect):
    """Build a connection mock with transaction() support for tenant_connection()."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock(return_value=None)  # set_config returns None
    conn.execute = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


@pytest.mark.asyncio
async def test_base_order_id_without_active_session_returns_none():
    """
    DUPLICATION FIX: No active session for the table → None.
    Prevents a new session from adding "Adicional #N" to a previous session.
    Updated: patches app.services.database.get_pool (tenant_connection pattern).
    """
    from app.repositories.tables_repo import db_get_base_order_id

    conn = _make_tenant_conn([
        None,  # no active session
        make_row({"base_id": "MESA-OLD"}),  # must not be reached
    ])
    pool = make_pool(conn)
    with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with _tenant_scope(1):
            result = await db_get_base_order_id("TBL-001")

    assert result is None
    # fetchrow call count: 1 set_config (fetchval) + 1 session query = fetchrow=1
    assert conn.fetchrow.call_count == 1  # solo 1 query: la de sesión


@pytest.mark.asyncio
async def test_base_order_id_with_active_session_returns_id():
    """With an active session → returns the base_order_id of the existing order."""
    from app.repositories.tables_repo import db_get_base_order_id

    conn = _make_tenant_conn([
        make_row({"id": "sess-001", "started_at": None}),
        make_row({"base_id": "MESA-AA3E4A"}),
    ])
    pool = make_pool(conn)
    with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with _tenant_scope(1):
            result = await db_get_base_order_id("TBL-001")

    assert result == "MESA-AA3E4A"


@pytest.mark.asyncio
async def test_base_order_id_active_session_without_orders_returns_none():
    """Active session but no previous orders → None (customer's first order)."""
    from app.repositories.tables_repo import db_get_base_order_id

    conn = _make_tenant_conn([
        make_row({"id": "sess-001", "started_at": None}),
        None,
    ])
    pool = make_pool(conn)
    with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with _tenant_scope(1):
            result = await db_get_base_order_id("TBL-001")

    assert result is None


@pytest.mark.asyncio
async def test_base_order_id_closed_session_does_not_reuse():
    """Table with a closed session (status != active) → None, doesn't reuse orders."""
    from app.repositories.tables_repo import db_get_base_order_id

    conn = _make_tenant_conn([
        None,  # Ninguna sesión activa (query filtra status='active')
        make_row({"base_id": "MESA-OLD"}),
    ])
    pool = make_pool(conn)
    with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with _tenant_scope(1):
            result = await db_get_base_order_id("TBL-001")

    assert result is None
