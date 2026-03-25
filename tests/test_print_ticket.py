"""
Tests para el endpoint GET /api/table-orders/{order_id}/ticket (FASE 3).
Cubre: agregación de sub-órdenes, datos fiscales opcionales, auth.
No requiere base de datos ni credenciales reales.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

import app.routes.tables as tables_routes


# ── Fixtures ──────────────────────────────────────────────────────────

MOCK_USER = {"username": "cajero", "branch_id": 5, "role": "caja"}

def _make_row(order_id, base_id, table_name, items, total, notes="", sub_number=1):
    """Crea un objeto asyncpg Row-like (dict envuelto en MagicMock)."""
    d = {
        "id":            order_id,
        "base_order_id": base_id,
        "table_name":    table_name,
        "items":         items,
        "total":         total,
        "notes":         notes,
        "sub_number":    sub_number,
        "station":       "all",
        "created_at":    datetime(2024, 6, 15, 12, 0, 0),
        "updated_at":    None,
        "status":        "factura_generada",
        "phone":         "+573001234567",
        "table_id":      1,
    }
    row = MagicMock()
    row.__iter__ = lambda s: iter(d.items())
    row.keys     = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get = lambda k, default=None: d.get(k, default)
    return row


# ══════════════════════════════════════════════════════════════════════
# 1. Endpoint /ticket — agregación de órdenes
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ticket_agrega_subordenes():
    """Múltiples sub-órdenes del mismo base_order_id deben agregarse en un solo ticket."""
    import json
    items1 = json.dumps([{"name": "Pizza", "price": 45000, "quantity": 2}])
    items2 = json.dumps([{"name": "Gaseosa", "price": 5000, "quantity": 3}])

    row1 = _make_row("BASE-001",   None,       "Mesa 5", items1, 90000, sub_number=1)
    row2 = _make_row("BASE-001-2", "BASE-001", "Mesa 5", items2, 15000, sub_number=2)

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[row1, row2])
    mock_conn.fetchrow = AsyncMock(return_value=None)  # sin factura fiscal

    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    mock_request = MagicMock()

    with (
        patch.object(tables_routes.db, "get_pool", AsyncMock(return_value=mock_pool)),
        patch("app.routes.deps.get_current_user", AsyncMock(return_value=MOCK_USER)),
        patch("app.routes.tables.get_current_user", AsyncMock(return_value=MOCK_USER)),
    ):
        result = await tables_routes.get_order_ticket(mock_request, "BASE-001")

    assert result["order_id"]   == "BASE-001"
    assert result["table_name"] == "Mesa 5"
    assert result["total"]      == 105000   # 90000 + 15000
    assert len(result["items"]) == 2        # Pizza + Gaseosa
    assert result["fiscal"]     is None


@pytest.mark.asyncio
async def test_ticket_orden_simple():
    """Una sola orden (sin sub-órdenes) devuelve sus datos correctamente."""
    import json
    items = json.dumps([{"name": "Bandeja Paisa", "price": 28000, "quantity": 1}])
    row   = _make_row("ORD-XYZ", None, "Mesa 2", items, 28000, notes="Sin picante")

    mock_conn = AsyncMock()
    mock_conn.fetch    = AsyncMock(return_value=[row])
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    mock_request = MagicMock()

    with (
        patch.object(tables_routes.db, "get_pool", AsyncMock(return_value=mock_pool)),
        patch("app.routes.deps.get_current_user", AsyncMock(return_value=MOCK_USER)),
        patch("app.routes.tables.get_current_user", AsyncMock(return_value=MOCK_USER)),
    ):
        result = await tables_routes.get_order_ticket(mock_request, "ORD-XYZ")

    assert result["total"]       == 28000
    assert result["notes"]       == "Sin picante"
    assert result["items"][0]["name"] == "Bandeja Paisa"


@pytest.mark.asyncio
async def test_ticket_orden_no_encontrada_retorna_404():
    """Si no hay órdenes con ese ID debe levantar HTTPException 404."""
    from fastapi import HTTPException

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])

    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    mock_request = MagicMock()

    with (
        patch.object(tables_routes.db, "get_pool", AsyncMock(return_value=mock_pool)),
        patch("app.routes.deps.get_current_user", AsyncMock(return_value=MOCK_USER)),
        patch("app.routes.tables.get_current_user", AsyncMock(return_value=MOCK_USER)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await tables_routes.get_order_ticket(mock_request, "ID-INEXISTENTE")

    assert exc_info.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# 2. Datos fiscales incluidos si existen
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ticket_incluye_fiscal_si_existe():
    """Si hay una factura emitida, el ticket debe incluir cufe, qr_data y dian_status."""
    import json
    items = json.dumps([{"name": "Ceviche", "price": 32000, "quantity": 1}])
    row   = _make_row("ORDER-FISCAL", None, "Mesa 7", items, 32000)

    fiscal_mock = {
        "cufe":           "a" * 96,
        "qr_data":        "https://catalogo-vpfe-hab.dian.gov.co/document/searchqr?documentkey=" + "a" * 96,
        "invoice_number": "FE990000001",
        "issue_date":     "2024-06-15",
        "tax_regime":     "iva",
        "tax_pct":        19.0,
        "dian_status":    "accepted",
        "uuid_dian":      "MOCK-FE990000001",
    }
    fiscal_row = MagicMock()
    fiscal_row.__iter__ = lambda s: iter(fiscal_mock.items())
    fiscal_row.keys     = lambda: fiscal_mock.keys()
    fiscal_row.__getitem__ = lambda s, k: fiscal_mock[k]

    mock_conn = AsyncMock()
    mock_conn.fetch    = AsyncMock(return_value=[row])
    mock_conn.fetchrow = AsyncMock(return_value=fiscal_row)

    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    mock_request = MagicMock()

    with (
        patch.object(tables_routes.db, "get_pool", AsyncMock(return_value=mock_pool)),
        patch("app.routes.deps.get_current_user", AsyncMock(return_value=MOCK_USER)),
        patch("app.routes.tables.get_current_user", AsyncMock(return_value=MOCK_USER)),
    ):
        result = await tables_routes.get_order_ticket(mock_request, "ORDER-FISCAL")

    assert result["fiscal"] is not None
    assert len(result["fiscal"]["cufe"]) == 96
    assert result["fiscal"]["dian_status"] == "accepted"
    assert result["fiscal"]["tax_regime"]  == "iva"


# ══════════════════════════════════════════════════════════════════════
# 3. Auth vía TestClient
# ══════════════════════════════════════════════════════════════════════

def test_ticket_sin_auth_retorna_401(client, monkeypatch):
    """Sin Bearer token válido el endpoint debe retornar 401."""
    from fastapi import HTTPException

    async def fake_verify_token(token: str):
        if not token:
            raise HTTPException(status_code=401, detail="No autenticado")
        return token

    monkeypatch.setattr("app.routes.deps.verify_token", fake_verify_token)
    # Llamada sin header Authorization → token vacío → 401
    response = client.get("/api/table-orders/cualquier-id/ticket")
    assert response.status_code == 401
