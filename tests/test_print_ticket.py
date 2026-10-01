"""
Tests for the GET /api/table-orders/{order_id}/ticket endpoint (PHASE 3).
Covers: sub-order aggregation, optional fiscal data, auth.
Does not require a database or real credentials.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

import app.routes.tables as tables_routes
from app.services.tenant_context import bypass_tenant_scope as _bypass


def _make_tenant_mock_conn():
    """Build a mock conn compatible with tenant_connection() (needs transaction() as sync ctx-mgr)."""
    conn = AsyncMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


# ── Fixtures ──────────────────────────────────────────────────────────

MOCK_USER = {"username": "cajero", "branch_id": 5, "role": "caja"}

def _make_row(order_id, base_id, table_name, items, total, notes="", sub_number=1):
    """Creates an asyncpg Row-like object (dict wrapped in a MagicMock)."""
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
# 1. /ticket endpoint — order aggregation
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ticket_aggregates_suborders():
    """Multiple sub-orders with the same base_order_id must be aggregated into a single ticket."""
    import json
    items1 = json.dumps([{"name": "Pizza", "price": 45000, "quantity": 2}])
    items2 = json.dumps([{"name": "Gaseosa", "price": 5000, "quantity": 3}])

    row1 = _make_row("BASE-001",   None,       "Mesa 5", items1, 90000, sub_number=1)
    row2 = _make_row("BASE-001-2", "BASE-001", "Mesa 5", items2, 15000, sub_number=2)

    mock_conn = _make_tenant_mock_conn()
    mock_conn.fetch = AsyncMock(return_value=[row1, row2])
    mock_conn.fetchrow = AsyncMock(return_value=None)  # no fiscal invoice

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
        _bypass("test: get_order_ticket direct call"),
    ):
        result = await tables_routes.get_order_ticket(mock_request, "BASE-001")

    assert result["order_id"]   == "BASE-001"
    assert result["table_name"] == "Mesa 5"
    assert result["total"]      == 105000   # 90000 + 15000
    assert len(result["items"]) == 2        # Pizza + Gaseosa
    assert result["fiscal"]     is None


@pytest.mark.asyncio
async def test_ticket_simple_order():
    """A single order (no sub-orders) returns its data correctly."""
    import json
    items = json.dumps([{"name": "Bandeja Paisa", "price": 28000, "quantity": 1}])
    row   = _make_row("ORD-XYZ", None, "Mesa 2", items, 28000, notes="Sin picante")

    mock_conn = _make_tenant_mock_conn()
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
        _bypass("test: get_order_ticket direct call"),
    ):
        result = await tables_routes.get_order_ticket(mock_request, "ORD-XYZ")

    assert result["total"]       == 28000
    assert result["notes"]       == "Sin picante"
    assert result["items"][0]["name"] == "Bandeja Paisa"


@pytest.mark.asyncio
async def test_ticket_order_not_found_returns_404():
    """If there are no orders with that ID it must raise HTTPException 404."""
    from fastapi import HTTPException

    mock_conn = _make_tenant_mock_conn()
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
        _bypass("test: get_order_ticket direct call"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await tables_routes.get_order_ticket(mock_request, "ID-INEXISTENTE")

    assert exc_info.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# 2. Fiscal data included if it exists
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ticket_includes_fiscal_if_exists():
    """If an invoice has been issued, the ticket must include cufe, qr_data and dian_status."""
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

    mock_conn = _make_tenant_mock_conn()
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
        _bypass("test: get_order_ticket direct call"),
    ):
        result = await tables_routes.get_order_ticket(mock_request, "ORDER-FISCAL")

    assert result["fiscal"] is not None
    assert len(result["fiscal"]["cufe"]) == 96
    assert result["fiscal"]["dian_status"] == "accepted"
    assert result["fiscal"]["tax_regime"]  == "iva"


# ══════════════════════════════════════════════════════════════════════
# 3. Auth via TestClient
# ══════════════════════════════════════════════════════════════════════

def test_ticket_without_auth_returns_401(client, monkeypatch):
    """Without a valid Bearer token the endpoint must return 401."""
    from fastapi import HTTPException

    async def fake_verify_token(token: str):
        if not token:
            raise HTTPException(status_code=401, detail="No autenticado")
        return token

    monkeypatch.setattr("app.routes.deps.verify_token", fake_verify_token)
    # Call without an Authorization header → empty token → 401
    response = client.get("/api/table-orders/cualquier-id/ticket")
    assert response.status_code == 401
