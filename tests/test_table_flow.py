import pytest
from unittest.mock import AsyncMock
import app.routes.tables as tables_routes

# ── FIXTURE: Fake database to avoid repeating code ──
@pytest.fixture
def mock_db_pool(monkeypatch):
    class MockConnection:
        async def fetchrow(self, query, *args):
            if "table_orders" in query:
                return {"phone": "573000000000", "table_name": "Mesa 1", "base_order_id": "MESA-TEST"}
            if "table_sessions" in query:
                return {"bot_number": "15556293573", "meta_phone_id": "123"}
            if "restaurants" in query:
                return {"id": 1, "name": "Restaurante Test", "whatsapp_number": "15556293573"}
            return None
        async def fetchval(self, query, *args): return None  # set_config calls
        async def execute(self, query, *args): pass

        def transaction(self):
            class _FakeTxn:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
            return _FakeTxn()

    class MockPool:
        def acquire(self): return self
        async def __aenter__(self): return MockConnection()
        async def __aexit__(self, exc_type, exc_val, exc_tb): pass

    # Inject the fake pool
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=MockPool()))
    # Bypass security — require_auth returns username
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value="caja_user"))
    # get_current_user must return a user with role 'caja' to pass _STATUS_ROLE_MAP
    monkeypatch.setattr(
        tables_routes, "get_current_user",
        AsyncMock(return_value={
            "username": "caja_user",
            "restaurant_name": "Test",
            "branch_id": 1,
            "role": "caja",
        }),
    )

# ── TEST 1: "Generate Invoice" button ──
@pytest.mark.asyncio
async def test_button_generate_invoice(client, monkeypatch, mock_db_pool):
    """Tests that 'generar_factura' calls db_mark_invoice_generated but does NOT clear the table."""

    mock_mark_factura = AsyncMock()
    monkeypatch.setattr(tables_routes.db, "db_mark_invoice_generated", mock_mark_factura)

    monkeypatch.setattr(tables_routes.db, "db_update_table_order_status", AsyncMock())

    headers = {"Authorization": "Bearer token_mesero"}
    response = client.post(
        "/api/table-orders/ORD-123/status",
        json={"status": "generar_factura"}, # <--- Simulating a click on Generate Invoice
        headers=headers
    )

    assert response.status_code == 200

    # VERIFY db_mark_invoice_generated WAS INDEED CALLED
    mock_mark_factura.assert_called_once_with("MESA-TEST")
    assert response.json()["status"] == "factura_generada"

# ── TEST 2: "Close Table" button ──
@pytest.mark.asyncio
async def test_button_close_table(client, monkeypatch, mock_db_pool):
    """Tests that 'cerrar_mesa' clears the DB and bids the customer farewell, but does NOT re-invoice."""

    from app.services import billing
    mock_emit_invoice = AsyncMock()
    monkeypatch.setattr(billing, "emit_invoice", mock_emit_invoice)

    mock_farewell = AsyncMock()
    monkeypatch.setattr(tables_routes, "_farewell_and_nps", mock_farewell)

    mock_close_bill = AsyncMock()
    monkeypatch.setattr(tables_routes.db, "db_close_table_bill", mock_close_bill)

    headers = {"Authorization": "Bearer token_mesero"}
    response = client.post(
        "/api/table-orders/ORD-123/status",
        json={"status": "cerrar_mesa"}, # <--- Simulating a click on Close Table
        headers=headers
    )

    assert response.status_code == 200

    # 1. VERIFY BILLING WAS NOT CALLED AGAIN
    mock_emit_invoice.assert_not_called()

    # 2. Verify the bill was indeed closed in the database
    mock_close_bill.assert_called_once_with("MESA-TEST")

    # 3. Verify the farewell function was called with the correct phone
    mock_farewell.assert_called_once()
    farewell_args, _ = mock_farewell.call_args
    assert farewell_args[0] == "573000000000"