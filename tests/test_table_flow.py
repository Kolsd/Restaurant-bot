import pytest
from unittest.mock import AsyncMock
import app.routes.tables as tables_routes

# ── FIXTURE: Base de Datos Falsa para no repetir código ──
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
        async def execute(self, query, *args): pass

    class MockPool:
        def acquire(self): return self
        async def __aenter__(self): return MockConnection()
        async def __aexit__(self, exc_type, exc_val, exc_tb): pass

    # Inyectamos el pool falso
    monkeypatch.setattr(tables_routes.db, "get_pool", AsyncMock(return_value=MockPool()))
    # Burlamos la seguridad
    monkeypatch.setattr(tables_routes, "require_auth", AsyncMock(return_value=True))

# ── PRUEBA 1: Botón "Generar Factura" ──
@pytest.mark.asyncio
async def test_boton_generar_factura(client, monkeypatch, mock_db_pool):
    """Prueba que 'generar_factura' llama a db_mark_factura_generada pero NO borra la mesa."""

    mock_mark_factura = AsyncMock()
    monkeypatch.setattr(tables_routes.db, "db_mark_factura_generada", mock_mark_factura)

    mock_send_wa = AsyncMock()
    monkeypatch.setattr(tables_routes, "send_wa_msg", mock_send_wa)
    monkeypatch.setattr(tables_routes.db, "db_update_table_order_status", AsyncMock())

    headers = {"Authorization": "Bearer token_mesero"}
    response = client.post(
        "/api/table-orders/ORD-123/status",
        json={"status": "generar_factura"}, # <--- Simulamos clic en Generar Factura
        headers=headers
    )

    assert response.status_code == 200

    # 1. VERIFICAMOS QUE SÍ SE LLAMÓ A db_mark_factura_generada
    mock_mark_factura.assert_called_once_with("MESA-TEST")

    # 2. Verificamos que se avisó por WhatsApp que la factura va en camino
    mock_send_wa.assert_called_once()
    wa_args, _ = mock_send_wa.call_args
    assert "Estamos preparando tu factura" in wa_args[1]

# ── PRUEBA 2: Botón "Cerrar Mesa" ──
@pytest.mark.asyncio
async def test_boton_cerrar_mesa(client, monkeypatch, mock_db_pool):
    """Prueba que 'cerrar_mesa' limpia la BD y despide al cliente, pero NO vuelve a facturar."""

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
        json={"status": "cerrar_mesa"}, # <--- Simulamos clic en Cerrar Mesa
        headers=headers
    )

    assert response.status_code == 200

    # 1. VERIFICAMOS QUE NO SE VOLVIÓ A LLAMAR A LA FACTURACIÓN
    mock_emit_invoice.assert_not_called()

    # 2. Verificamos que sí se haya cerrado la cuenta en base de datos
    mock_close_bill.assert_called_once_with("MESA-TEST")

    # 3. Verificamos que la función de despedida fue llamada con el teléfono correcto
    mock_farewell.assert_called_once()
    farewell_args, _ = mock_farewell.call_args
    assert farewell_args[0] == "573000000000"