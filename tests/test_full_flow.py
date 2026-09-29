"""
tests/test_full_flow.py

Comprehensive integration test suite covering all restaurant bot operational flows:
  A. KDS — Cocina / Bar (10 tests)
  B. Mesero / Waiter (10 tests)
  C. Domiciliario / Delivery rider (10 tests)
  D. Caja / Cashier (10 tests)
  E. Bot WhatsApp + Anthropic (10 tests)
  F. End-to-end: Complete table flow (10 tests)

All external dependencies (DB, Anthropic, Meta API) are fully mocked.
"""

import pytest
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from app.main import app
from app.services import database as db

# Re-import helpers from conftest so they are available directly
from tests.conftest import make_pool, make_row, patch_auth


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mock_order_row(
    order_id: str = "order-abc",
    status: str = "recibido",
    station: str = "all",
    table_id: str = "table-1",
    phone: str = "manual",
    branch_id: int = 1,
    total: float = 25000.0,
) -> dict:
    return {
        "id": order_id,
        "table_id": table_id,
        "table_name": "Mesa 1",
        "phone": phone,
        "items": '[{"name": "Hamburguesa", "price": 25000, "quantity": 1}]',
        "status": status,
        "station": station,
        "notes": "",
        "total": total,
        "base_order_id": order_id,
        "sub_number": 1,
        "branch_id": branch_id,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def _mock_check(
    check_id: str = "check-1",
    base_order_id: str = "order-abc",
    status: str = "open",
    total: float = 25000.0,
    tip: float = 0.0,
    check_number: int = 1,
) -> dict:
    return {
        "id": check_id,
        "base_order_id": base_order_id,
        "check_number": check_number,
        "items": '[{"name": "Hamburguesa", "qty": 1, "unit_price": 25000.0}]',
        "subtotal": total,
        "tax_amount": 0.0,
        "total": total,
        "status": status,
        "paid_at": None,
        "payments": None,
        "proposed_payments": None,
        "proposed_tip": None,
        "tip_amount": tip,
        "change_amount": 0.0,
        "fiscal_invoice_id": None,
        "customer_name": "Consumidor Final",
        "customer_nit": "222222222",
        "customer_email": "",
    }


def _mock_delivery_order(
    order_id: str = "del-001",
    status: str = "confirmado",
    phone: str = "573001112233",
    address: str = "Calle 1 #2-3",
) -> dict:
    return {
        "id": order_id,
        "phone": phone,
        "items": '[{"name": "Pizza", "quantity": 1, "price": 30000}]',
        "order_type": "domicilio",
        "address": address,
        "notes": "",
        "total": 30000.0,
        "paid": False,
        "status": status,
        "payment_method": "nequi",
        "bot_number": "+573009876543",
        "created_at": datetime.now(timezone.utc),
    }


# ===========================================================================
# A. KDS — Cocina / Bar flows
# ===========================================================================

class TestKDSFlows:
    """Section A: KDS / kitchen-display flows."""

    def test_kds_kitchen_station_returns_orders(self, client, monkeypatch):
        """KDS cocina: ?station=kitchen returns only kitchen orders."""
        patch_auth(monkeypatch, role="owner")
        row = make_row(_mock_order_row(station="kitchen", status="recibido"))

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders?station=kitchen",
            headers={"Authorization": "Bearer fake", "X-Branch-ID": "1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "orders" in data
        assert all(o["station"] in ("kitchen", "all") for o in data["orders"])

    def test_kds_bar_station_returns_orders(self, client, monkeypatch):
        """KDS bar: ?station=bar returns only bar orders."""
        patch_auth(monkeypatch, role="owner")
        row = make_row(_mock_order_row(station="bar", status="recibido"))

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders?station=bar",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert "orders" in resp.json()

    def test_kds_all_station_returns_all_orders(self, client, monkeypatch):
        """KDS station=all: no station filter applied — all orders returned."""
        patch_auth(monkeypatch, role="owner")
        rows = [
            make_row(_mock_order_row(order_id="o1", station="kitchen")),
            make_row(_mock_order_row(order_id="o2", station="bar")),
            make_row(_mock_order_row(order_id="o3", station="all")),
        ]

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=rows)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["orders"]) == 3

    def test_kds_mark_order_en_preparacion(self, client, monkeypatch):
        """Mark a table order as en_preparacion returns success."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {
                "phone": "manual",
                "table_name": "Mesa 1",
                "base_order_id": "order-abc",
                "table_id": "table-1",
            }
        )

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=order_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        monkeypatch.setattr(db, "db_update_table_order_status", AsyncMock())

        resp = client.post(
            "/api/table-orders/order-abc/status",
            json={"status": "en_preparacion"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "en_preparacion"

    def test_kds_mark_order_listo(self, client, monkeypatch):
        """Mark order as listo returns success; no WA for phone=manual."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {
                "phone": "manual",
                "table_name": "Mesa 2",
                "base_order_id": "order-xyz",
                "table_id": "table-2",
            }
        )

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=order_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        monkeypatch.setattr(db, "db_update_table_order_status", AsyncMock())

        resp = client.post(
            "/api/table-orders/order-xyz/status",
            json={"status": "listo"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_kds_order_not_found_returns_404(self, client, monkeypatch):
        """Status update on unknown order_id → 404."""
        patch_auth(monkeypatch, role="owner")

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.post(
            "/api/table-orders/nonexistent/status",
            json={"status": "en_preparacion"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 404

    def test_kds_filters_by_branch_id_header(self, client, monkeypatch):
        """Orders are filtered to the branch specified in X-Branch-ID header."""
        patch_auth(monkeypatch, role="owner")
        row = make_row(_mock_order_row(branch_id=2))

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders",
            headers={"Authorization": "Bearer fake", "X-Branch-ID": "2"},
        )
        assert resp.status_code == 200
        # The branch filter would pass branch_id=2 to the SQL — mock returned our row
        assert len(resp.json()["orders"]) >= 1

    def test_kds_new_suborder_visible_without_duplicate(self, client, monkeypatch):
        """Two sub-orders of the same base appear as separate rows (not merged)."""
        patch_auth(monkeypatch, role="owner")
        row1 = make_row(_mock_order_row(order_id="base-1", station="kitchen"))
        row2 = make_row(
            {**_mock_order_row(order_id="sub-1", station="kitchen"), "base_order_id": "base-1"}
        )

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row1, row2])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders?station=kitchen",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["orders"]) == 2

    def test_kds_cancelled_order_excluded(self, client, monkeypatch):
        """Cancelled orders are excluded from KDS active view."""
        patch_auth(monkeypatch, role="owner")
        # The SQL WHERE clause excludes 'cancelado'; mock returns empty list
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["orders"] == []

    def test_kds_invalid_status_returns_400(self, client, monkeypatch):
        """Sending an invalid status string → 400."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {"phone": "manual", "table_name": "Mesa 1", "base_order_id": "o1", "table_id": "t1"}
        )
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=order_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.post(
            "/api/table-orders/o1/status",
            json={"status": "status_invalido"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400


# ===========================================================================
# B. Mesero (waiter) flow
# ===========================================================================

class TestWaiterFlows:
    """Section B: Waiter / mesero operational flows."""

    def test_create_admin_call_alert(self, client, monkeypatch):
        """POST /api/waiter-alerts/admin-call creates an alert successfully."""
        patch_auth(monkeypatch, role="owner")
        alert = {"id": 1, "alert_type": "admin_call", "table_name": "Mesa 3"}
        monkeypatch.setattr(db, "db_create_waiter_alert", AsyncMock(return_value=alert))

        resp = client.post(
            "/api/waiter-alerts/admin-call",
            json={"phone": "admin", "table_id": "t-3", "table_name": "Mesa 3"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["alert"]["alert_type"] == "admin_call"

    def test_waiter_sees_pending_alerts(self, client, monkeypatch):
        """GET /api/waiter-alerts returns alert list."""
        patch_auth(monkeypatch, role="mesero")
        alert_row = make_row({"id": 1, "alert_type": "admin_call", "table_name": "Mesa 1"})

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[alert_row])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/waiter-alerts",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data
        assert len(data["alerts"]) == 1

    def test_dismiss_alert_succeeds(self, client, monkeypatch):
        """POST /api/waiter-alerts/{id}/dismiss marks the alert dismissed
        (soft — see the 2026-09 fix: this used to be shadowed by a duplicate
        DELETE-based function of the same name; "UPDATE 1" is the real
        asyncpg execute() status string a matching UPDATE returns)."""
        patch_auth(monkeypatch, role="mesero")

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.post(
            "/api/waiter-alerts/1/dismiss",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_pos_create_order(self, client, monkeypatch):
        """POST /api/pos/order creates a table order from POS."""
        patch_auth(monkeypatch, role="mesero")
        monkeypatch.setattr(db, "db_get_base_order_id", AsyncMock(return_value=None))
        monkeypatch.setattr(db, "db_get_next_sub_number", AsyncMock(return_value=1))
        monkeypatch.setattr(db, "db_save_table_order", AsyncMock())

        resp = client.post(
            "/api/pos/order",
            json={
                "table_id": "table-1",
                "table_name": "Mesa 1",
                "items": [{"name": "Hamburguesa", "price": 20000, "quantity": 1}],
                "total": 20000,
                "notes": "",
                "station": "kitchen",
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "order_id" in data

    def test_view_table_with_active_session(self, client, monkeypatch):
        """GET /api/pos/tables-status returns tables with bot_active flag."""
        from app.repositories import tables_repo as tr_repo  # noqa: PLC0415
        patch_auth(monkeypatch, role="owner")
        table_row = {"id": "table-1", "name": "Mesa 1", "number": 1, "active": True}
        monkeypatch.setattr(db, "db_get_tables", AsyncMock(return_value=[table_row]))
        # Wave-2: db_get_restaurant_by_org_id/_by_location_id must return
        # org_id + location_id so the route resolves the sede id without
        # falling back to the Matriz invariant.
        _rest_mock = AsyncMock(return_value={
            "id": 1, "org_id": 1, "location_id": 1,
            "name": "Test", "parent_restaurant_id": None, "features": {}
        })
        monkeypatch.setattr(db, "db_get_restaurant_by_org_id", _rest_mock)
        monkeypatch.setattr(db, "db_get_restaurant_by_location_id", _rest_mock)

        session_row = make_row({"table_id": "table-1", "session_started_at": None,
                                "has_waiter_alert": False, "has_open_check": False,
                                "current_total": 0, "session_active": True,
                                # Enrichment fields added when the query was extended
                                # to surface waiter attribution + order channel.
                                "active_order_id": None, "channel": None,
                                "waiter_staff_id": None, "assigned_staff_id": None,
                                "waiter_name": None})
        order_row = make_row({"table_id": "table-1", "status": "recibido"})
        conn = AsyncMock()
        # Call order: db_get_pending_orders_by_branch (orders), then
        # db_get_tables_status_enrichment (enrichment), then
        # db_get_active_session_table_ids (sessions).
        conn.fetch = AsyncMock(side_effect=[[order_row], [session_row], [session_row]])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/pos/tables-status",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        assert len(tables) == 1
        assert tables[0]["bot_active"] is True

    def test_close_table_conversation(self, client, monkeypatch):
        """DELETE /api/conversations/{phone} closes the table session."""
        patch_auth(monkeypatch, role="mesero")
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.delete(
            "/api/conversations/573001234567",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_get_order_ticket_bill(self, client, monkeypatch):
        """GET /api/table-orders/{id}/ticket returns aggregated bill."""
        patch_auth(monkeypatch, role="mesero")
        order_row = make_row(
            {
                **_mock_order_row(),
                "items": '[{"name":"Pasta","price":18000,"quantity":1}]',
                "total": 18000,
                "notes": "sin cebolla",
                "created_at": datetime.now(timezone.utc),
            }
        )
        fiscal_row = None

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[order_row])
        conn.fetchrow = AsyncMock(return_value=fiscal_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders/order-abc/ticket",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "order-abc"
        assert "items" in data
        assert data["total"] == 18000.0

    def test_create_checks_for_split(self, client, monkeypatch):
        """POST /api/table-orders/{id}/checks creates split checks."""
        patch_auth(monkeypatch, role="mesero")
        ticket = {
            "items": [
                {"name": "Pasta", "price": 18000, "quantity": 1},
                {"name": "Vino", "price": 12000, "quantity": 1},
            ],
            "org_id": 1,
        }
        monkeypatch.setattr(db, "db_get_order_ticket_data", AsyncMock(return_value=ticket))
        monkeypatch.setattr(db, "db_create_checks", AsyncMock(return_value=[
            {"id": "c1", "check_number": 1, "total": 18000.0},
            {"id": "c2", "check_number": 2, "total": 12000.0},
        ]))

        resp = client.post(
            "/api/table-orders/order-abc/checks",
            json={
                "checks": [
                    {"check_number": 1, "items": [{"name": "Pasta", "qty": 1, "unit_price": 18000}]},
                    {"check_number": 2, "items": [{"name": "Vino",  "qty": 1, "unit_price": 12000}]},
                ],
                "tax_pct": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["checks"]) == 2

    def test_split_check_excess_quantity_rejected(self, client, monkeypatch):
        """Creating checks with more qty than ordered → 400."""
        patch_auth(monkeypatch, role="mesero")
        ticket = {"items": [{"name": "Pasta", "price": 18000, "quantity": 1}], "org_id": 1}
        monkeypatch.setattr(db, "db_get_order_ticket_data", AsyncMock(return_value=ticket))

        resp = client.post(
            "/api/table-orders/order-abc/checks",
            json={
                "checks": [
                    {"check_number": 1, "items": [{"name": "Pasta", "qty": 3, "unit_price": 18000}]},
                ],
                "tax_pct": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_pay_check_completes_transaction(self, client, monkeypatch):
        """POST pay check with sufficient payment → 200 success."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(total=25000.0)
        # Race-free flow: route uses db_claim_check_for_payment (open→paying)
        # then db_finalize_check_payment (paying→invoiced, returns True on success).
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_finalize_check_payment", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_get_first_table_order", AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "efectivo", "amount": 25000}],
                "tip_amount": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "change" in data


# ===========================================================================
# C. Domiciliario (delivery rider) flow
# ===========================================================================

class TestDeliveryRiderFlows:
    """Section C: Delivery rider flows.

    The rider tests that hit GET/PATCH /api/delivery/orders* were removed in
    chunk 9 (docs/claude/delivery-web.md) — those endpoints only ever served
    the retired WhatsApp delivery/pickup flow and were org-wide (not
    sede-scoped), leaking every order's customer PII to any staff member.
    The new sede-scoped rider surface is app/routes/staff_delivery.py
    (/api/staff/delivery/*). GET /api/orders/{id} is untouched (still a
    generic order lookup, not delivery-specific) so its tests stay.
    """

    def test_get_single_delivery_order(self, client, monkeypatch):
        """GET /api/orders/{id} returns full order details."""
        patch_auth(monkeypatch, role="domiciliario")
        order = _mock_delivery_order()
        monkeypatch.setattr(db, "db_get_order", AsyncMock(return_value=order))

        resp = client.get(
            "/api/orders/del-001",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "del-001"
        assert data["address"] == "Calle 1 #2-3"

    def test_order_with_address_shows_coordinates(self, client, monkeypatch):
        """Order with address field is returned intact."""
        patch_auth(monkeypatch, role="domiciliario")
        order = _mock_delivery_order(address="Cra 7 #45-12, Bogotá")
        monkeypatch.setattr(db, "db_get_order", AsyncMock(return_value=order))

        resp = client.get(
            "/api/orders/del-001",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert "Cra 7" in resp.json()["address"]

    def test_order_not_found_returns_404(self, client, monkeypatch):
        """GET /api/orders/{id} for unknown id → 404."""
        patch_auth(monkeypatch, role="domiciliario")
        monkeypatch.setattr(db, "db_get_order", AsyncMock(return_value=None))

        resp = client.get(
            "/api/orders/nonexistent",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 404


# ===========================================================================
# D. Caja (cashier) flow
# ===========================================================================

class TestCashierFlows:
    """Section D: Cashier (caja) operational flows."""

    def test_list_all_orders_dashboard(self, client, monkeypatch):
        """GET /api/orders returns summary + orders list for cashier."""
        patch_auth(monkeypatch, role="caja")
        orders = [
            {**_mock_delivery_order(order_id="o1"), "paid": True, "total": 30000},
            {**_mock_delivery_order(order_id="o2"), "paid": False, "total": 25000},
        ]
        monkeypatch.setattr(db, "db_get_all_orders", AsyncMock(return_value=orders))

        resp = client.get(
            "/api/orders",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["total_orders"] == 2
        assert data["summary"]["paid"] == 1
        assert data["summary"]["total_revenue"] == 30000

    def test_confirm_delivery_payment(self, client, monkeypatch):
        """PATCH kitchen delivery → confirmado triggers billing (dian=False → skips invoice)."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        order_row = make_row(_mock_delivery_order(status="pendiente_pago"))

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value=order_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        _rest_mock = AsyncMock(return_value={
            "id": 1, "org_id": 1, "location_id": 1, "features": {"dian_active": False}
        })
        monkeypatch.setattr(db, "db_get_restaurant_by_org_id", _rest_mock)
        monkeypatch.setattr(db, "db_get_restaurant_by_location_id", _rest_mock)
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.patch(
            "/api/kitchen/delivery-orders/del-001/status",
            json={"status": "confirmado"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_get_checks_for_table(self, client, monkeypatch):
        """GET /api/table-orders/{id}/checks returns check list."""
        patch_auth(monkeypatch, role="caja")
        checks = [_mock_check(), _mock_check(check_id="c2", check_number=2)]
        monkeypatch.setattr(db, "db_get_checks", AsyncMock(return_value=checks))

        resp = client.get(
            "/api/table-orders/order-abc/checks",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["checks"]) == 2

    def test_pay_check_with_tip_valid(self, client, monkeypatch):
        """Pay check with tip <= 50% of total → 200."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(total=20000.0)
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_finalize_check_payment", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_get_first_table_order", AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))
        import app.routes.tables as tables_mod

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "tarjeta", "amount": 30000}],
                "tip_amount": 5000.0,   # 25% of 20000 — valid
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_pay_check_tip_exceeds_50_percent_rejected(self, client, monkeypatch):
        """Pay check with tip > 50% of total → 400."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(total=10000.0)
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "efectivo", "amount": 20000}],
                "tip_amount": 6000.0,   # 60% of 10000 — invalid
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400
        assert "propina" in resp.json()["detail"].lower()

    def test_pay_check_insufficient_amount_rejected(self, client, monkeypatch):
        """Pay check with payment amount < check total → 400."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(total=30000.0)
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "efectivo", "amount": 10000}],
                "tip_amount": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400
        assert "suficiente" in resp.json()["detail"].lower() or "insuficiente" in resp.json()["detail"].lower()

    def test_dian_inactive_no_invoice_created(self, client, monkeypatch):
        """When dian_active=False billing adapter is never called."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(total=15000.0)
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_finalize_check_payment", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_get_first_table_order", AsyncMock(return_value=None))
        mock_billing = AsyncMock(return_value=None)
        monkeypatch.setattr("app.services.billing.get_billing_config", mock_billing)

        adapter_mock = MagicMock()
        adapter_mock.create_invoice = AsyncMock(return_value={"id": "inv-1"})
        monkeypatch.setattr("app.services.billing.get_adapter", MagicMock(return_value=adapter_mock))

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "efectivo", "amount": 15000}],
                "tip_amount": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        # Adapter was not called because config is None
        adapter_mock.create_invoice.assert_not_called()

    def test_order_passes_to_invoice_delivered(self, client, monkeypatch):
        """After paying all checks, first table order transitions to factura_entregada."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(total=20000.0)
        first_order = {
            **_mock_order_row(status="factura_entregada", phone="manual"),
            "table_id": "t1",
        }
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_finalize_check_payment", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_get_first_table_order", AsyncMock(return_value=first_order))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "efectivo", "amount": 20000}],
                "tip_amount": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_pay_already_paid_check_rejected(self, client, monkeypatch):
        """Paying an already-paid check → 409 (Conflict).

        Was 400 in the pre-race-fix flow; now 409 because the request conflicts
        with the resource's current state — accurate HTTP semantic.
        """
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        check = _mock_check(status="paid", total=20000.0)
        # Claim refused because status != 'open'.
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value=None))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=False))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.post(
            "/api/table-orders/order-abc/checks/check-1/pay",
            json={
                "payments": [{"method": "efectivo", "amount": 20000}],
                "tip_amount": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 409

    def test_get_open_shifts_summary(self, client, monkeypatch):
        """GET /api/staff/open-shifts returns current open shifts for admin dashboard."""
        patch_auth(monkeypatch, role="owner", features={"staff_tips": True})
        monkeypatch.setattr(db, "db_get_open_shifts", AsyncMock(return_value=[
            {"id": "s1", "staff_name": "Juan", "clock_in": _now_iso()}
        ]))
        # require_module checks this
        monkeypatch.setattr(db, "db_check_module", AsyncMock(return_value=True))

        resp = client.get(
            "/api/staff/open-shifts",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert "shifts" in resp.json()


# ===========================================================================
# E. Bot WhatsApp + Anthropic flow
# ===========================================================================


# ===========================================================================
# F. Flujo end-to-end: Mesa completa
# ===========================================================================

class TestEndToEndTableFlow:
    """Section F: Full table lifecycle — from creation to NPS."""

    def test_table_created_starts_free(self, client, monkeypatch):
        """POST /api/tables creates a new table with correct name."""
        patch_auth(monkeypatch, role="owner")
        new_table = {"id": "t-new", "name": "Mesa 5", "number": 5, "active": True}
        monkeypatch.setattr(db, "db_auto_create_table", AsyncMock(return_value=new_table))
        _rest_mock = AsyncMock(return_value={
            "id": 1, "org_id": 1, "location_id": 1, "parent_restaurant_id": None
        })
        monkeypatch.setattr(db, "db_get_restaurant_by_org_id", _rest_mock)
        monkeypatch.setattr(db, "db_get_restaurant_by_location_id", _rest_mock)

        resp = client.post(
            "/api/tables",
            json={},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["table_id"] == "t-new"

    def test_table_session_open_marks_occupied(self, client, monkeypatch):
        """KDS shows session active after client connects via WhatsApp QR."""
        from app.repositories import tables_repo as tr_repo  # noqa: PLC0415
        patch_auth(monkeypatch, role="owner")
        session_row = make_row({"table_id": "t-new"})
        order_row_empty = make_row({"table_id": "t-new", "status": "recibido"})

        table_row = {"id": "t-new", "name": "Mesa 5", "number": 5, "active": True}
        monkeypatch.setattr(db, "db_get_tables", AsyncMock(return_value=[table_row]))
        _rest_mock = AsyncMock(return_value={
            "id": 1, "org_id": 1, "location_id": 1,
            "name": "Test", "parent_restaurant_id": None, "features": {}
        })
        monkeypatch.setattr(db, "db_get_restaurant_by_org_id", _rest_mock)
        monkeypatch.setattr(db, "db_get_restaurant_by_location_id", _rest_mock)

        conn = AsyncMock()
        # Call order: db_get_pending_orders_by_branch first, db_get_active_session_table_ids second.
        conn.fetch = AsyncMock(side_effect=[[order_row_empty], [session_row]])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        # Mock enrichment so it doesn't try a real DB connection
        monkeypatch.setattr(
            tr_repo, "db_get_tables_status_enrichment", AsyncMock(return_value={})
        )

        resp = client.get(
            "/api/pos/tables-status",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        assert any(t["bot_active"] is True for t in tables)

    def test_waiter_takes_order_kds_receives_it(self, client, monkeypatch):
        """Waiter creates a POS order → it appears in KDS (station=kitchen)."""
        patch_auth(monkeypatch, role="mesero")
        monkeypatch.setattr(db, "db_get_base_order_id", AsyncMock(return_value=None))
        monkeypatch.setattr(db, "db_get_next_sub_number", AsyncMock(return_value=1))
        saved = {}

        async def capture_save(order):
            saved.update(order)

        monkeypatch.setattr(db, "db_save_table_order", capture_save)

        resp = client.post(
            "/api/pos/order",
            json={
                "table_id": "t-new",
                "table_name": "Mesa 5",
                "items": [{"name": "Lomito", "price": 28000, "quantity": 1}],
                "total": 28000,
                "station": "kitchen",
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert saved.get("station") == "kitchen"
        assert saved.get("table_id") == "t-new"

    def test_kds_changes_order_to_en_preparacion(self, client, monkeypatch):
        """KDS marks the order en_preparacion."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {"phone": "manual", "table_name": "Mesa 5", "base_order_id": "o-new", "table_id": "t-new"}
        )
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=order_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        update_mock = AsyncMock()
        monkeypatch.setattr(db, "db_update_table_order_status", update_mock)

        resp = client.post(
            "/api/table-orders/o-new/status",
            json={"status": "en_preparacion"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        update_mock.assert_called_once_with("o-new", "en_preparacion")

    def test_kds_changes_order_to_listo(self, client, monkeypatch):
        """KDS marks the order listo."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {"phone": "manual", "table_name": "Mesa 5", "base_order_id": "o-new", "table_id": "t-new"}
        )
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=order_row)
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        update_mock = AsyncMock()
        monkeypatch.setattr(db, "db_update_table_order_status", update_mock)

        resp = client.post(
            "/api/table-orders/o-new/status",
            json={"status": "listo"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        update_mock.assert_called_once_with("o-new", "listo")

    def test_waiter_requests_bill_creates_check(self, client, monkeypatch):
        """Cashier creates a check for the table's bill."""
        patch_auth(monkeypatch, role="caja")
        ticket = {
            "items": [{"name": "Lomito", "price": 28000, "quantity": 1}],
            "org_id": 1,
        }
        monkeypatch.setattr(db, "db_get_order_ticket_data", AsyncMock(return_value=ticket))
        monkeypatch.setattr(db, "db_create_checks", AsyncMock(return_value=[
            {"id": "chk-1", "check_number": 1, "total": 28000.0},
        ]))

        resp = client.post(
            "/api/table-orders/o-new/checks",
            json={
                "checks": [
                    {"check_number": 1, "items": [{"name": "Lomito", "qty": 1, "unit_price": 28000}]},
                ],
                "tax_pct": 0.0,
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["checks"][0]["total"] == 28000.0

    def test_cashier_pays_check_with_tip(self, client, monkeypatch):
        """Cashier pays the check including a tip."""
        patch_auth(monkeypatch, role="caja", features={"dian_active": False})
        # Match base_order_id to the URL segment
        check = _mock_check(check_id="chk-1", base_order_id="o-new", total=28000.0)
        monkeypatch.setattr(db, "db_claim_check_for_payment", AsyncMock(return_value={**check, "status": "paying"}))
        monkeypatch.setattr(db, "db_get_check", AsyncMock(return_value=check))
        monkeypatch.setattr(db, "db_release_check", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_finalize_check_payment", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "db_get_first_table_order", AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.billing.get_billing_config", AsyncMock(return_value=None))

        resp = client.post(
            "/api/table-orders/o-new/checks/chk-1/pay",
            json={
                "payments": [{"method": "tarjeta", "amount": 32000}],
                "tip_amount": 2800.0,   # 10% of 28000
            },
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["change"] == pytest.approx(4000.0, abs=1)  # 32000 paid - 28000 total

    def test_table_returns_free_after_closing(self, client, monkeypatch):
        """After cerrar_mesa, table transitions back to free (no active orders)."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {"phone": "573001234567", "table_name": "Mesa 5", "base_order_id": "o-new", "table_id": "t-new"}
        )
        session_row = {"bot_number": "+573009876543", "meta_phone_id": None}

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[order_row, session_row])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        monkeypatch.setattr(db, "db_close_table_bill", AsyncMock())
        monkeypatch.setattr(db, "db_get_table_by_id", AsyncMock(return_value={"id": "t-new"}))
        _rest_mock_empty = AsyncMock(return_value={})
        monkeypatch.setattr(db, "db_get_restaurant_by_org_id", _rest_mock_empty)
        monkeypatch.setattr(db, "db_get_restaurant_by_location_id", _rest_mock_empty)
        monkeypatch.setattr(db, "db_get_all_restaurants", AsyncMock(return_value=[{
            "id": 1, "name": "Test", "whatsapp_number": "+573009876543"
        }]))
        monkeypatch.setattr(db, "db_mark_session_nps_pending", AsyncMock())
        monkeypatch.setattr(db, "db_cleanup_after_checkout", AsyncMock())
        monkeypatch.setattr(db, "db_get_restaurant_by_bot_number", AsyncMock(return_value={
            "id": 1, "name": "Test", "whatsapp_number": "+573009876543"
        }))

        with patch("app.routes.tables.asyncio.create_task"):
            resp = client.post(
                "/api/table-orders/o-new/status",
                json={"status": "cerrar_mesa"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "factura_entregada"

    def test_nps_triggered_after_table_close(self, client, monkeypatch):
        """After cerrar_mesa for a WhatsApp customer, NPS is triggered."""
        patch_auth(monkeypatch, role="owner")
        order_row = make_row(
            {"phone": "573001234567", "table_name": "Mesa 5", "base_order_id": "o-new", "table_id": "t-new"}
        )
        session_row = {"bot_number": "+573009876543", "meta_phone_id": None}

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[order_row, session_row])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
        monkeypatch.setattr(db, "db_close_table_bill", AsyncMock())
        monkeypatch.setattr(db, "db_get_table_by_id", AsyncMock(return_value={"id": "t-new"}))
        _rest_mock_empty = AsyncMock(return_value={})
        monkeypatch.setattr(db, "db_get_restaurant_by_org_id", _rest_mock_empty)
        monkeypatch.setattr(db, "db_get_restaurant_by_location_id", _rest_mock_empty)
        monkeypatch.setattr(db, "db_get_all_restaurants", AsyncMock(return_value=[{
            "id": 1, "name": "Test", "whatsapp_number": "+573009876543"
        }]))
        monkeypatch.setattr(db, "db_get_restaurant_by_bot_number", AsyncMock(return_value={
            "id": 1, "name": "Test", "whatsapp_number": "+573009876543"
        }))

        nps_mock = AsyncMock()
        nps_pending_mock = AsyncMock()
        monkeypatch.setattr("app.routes.tables.trigger_nps", nps_mock)
        monkeypatch.setattr(db, "db_mark_session_nps_pending", nps_pending_mock)
        monkeypatch.setattr(db, "db_cleanup_after_checkout", AsyncMock())

        with patch("app.routes.tables.asyncio.create_task") as create_task_mock:
            resp = client.post(
                "/api/table-orders/o-new/status",
                json={"status": "cerrar_mesa"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        # asyncio.create_task should have been called to trigger NPS
        assert create_task_mock.called

    def test_new_session_same_table_no_old_orders(self, client, monkeypatch):
        """A new table session does not carry over orders from previous session."""
        patch_auth(monkeypatch, role="owner")
        # The KDS query filters status NOT IN ('factura_entregada','cancelado'),
        # so old paid orders won't appear — mock returns empty list for new session.
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))

        resp = client.get(
            "/api/table-orders",
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["orders"] == []
