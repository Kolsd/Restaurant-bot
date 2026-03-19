import pytest
from unittest.mock import AsyncMock
import app.routes.tables as tables_routes

@pytest.mark.asyncio
async def test_waiter_closes_table_and_triggers_billing(client, monkeypatch):
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value=True))

    class MockConnection:
        async def fetchrow(self, query, *args):
            if "table_orders" in query: 
                return {"phone": "573000000000", "table_name": "Mesa 1", "base_order_id": "MESA-TEST"}
            if "table_sessions" in query:
                return {"bot_number": "15556293573", "meta_phone_id": "123"}
            if "restaurants" in query:  # <--- ¡AQUÍ ESTÁ EL FIX! Le damos el restaurante
                return {"id": 1, "name": "Restaurante Test", "whatsapp_number": "15556293573"}
            return None
            
        async def execute(self, query, *args): pass

    class MockPool:
        def acquire(self): return self
        async def __aenter__(self): return MockConnection()
        async def __aexit__(self, exc_type, exc_val, exc_tb): pass

    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=MockPool()))

    from app.services import billing
    mock_emit_invoice = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(billing, "emit_invoice", mock_emit_invoice)
    monkeypatch.setattr(tables_routes, "send_wa_msg", AsyncMock())
    
    headers = {"Authorization": "Bearer token_mesero"}
    response = client.post(
        "/api/table-orders/ORD-123/status", 
        json={"status": "factura_entregada"},
        headers=headers
    )

    assert response.status_code == 200
    
    # ¡Ahora sí debe ser llamado!
    mock_emit_invoice.assert_called_once()
    args, _ = mock_emit_invoice.call_args
    assert args[0] == "MESA-TEST" # Verificamos que pase el ID correcto
    assert args[1] == 1 # Verificamos que pase el ID del restaurante (1)