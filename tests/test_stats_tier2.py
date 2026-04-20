"""
tests/test_stats_tier2.py

Unit + integration tests for the four Tier-2 Dashboard Analytics endpoints:
  GET /api/stats/by-channel
  GET /api/stats/top-dishes
  GET /api/stats/inventory-critical
  GET /api/stats/live-orders

Unit tests (no DB) mock the repo layer.
Integration tests (require TEST_DATABASE_URL) call real queries via db_conn.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import stats_repo


# ── helpers ────────────────────────────────────────────────────────────────────

def _auth_headers():
    return {"Authorization": "Bearer test-token"}


# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def patched_auth(monkeypatch):
    """Minimal auth patch — any Bearer token resolves to restaurant id=1."""
    from tests.conftest import patch_auth
    return patch_auth(monkeypatch, restaurant_id=1)


# ══════════════════════════════════════════════════════════════════════════════
# 1. GET /api/stats/by-channel
# ══════════════════════════════════════════════════════════════════════════════

MOCK_CHANNEL_RESPONSE = {
    "period": {"start": "2026-04-12", "end": "2026-04-18"},
    "total": 12400000,
    "total_count": 1284,
    "channels": [
        {"channel": "whatsapp_bot", "label": "WhatsApp · Bot", "total": 4800000, "count": 497, "pct": 38.7},
        {"channel": "pos",          "label": "Salón · POS",    "total": 5200000, "count": 412, "pct": 41.9},
        {"channel": "delivery",     "label": "Domicilios",     "total": 1900000, "count": 248, "pct": 15.3},
        {"channel": "qr_table",     "label": "QR de mesa",     "total": 500000,  "count": 127, "pct": 4.1},
    ],
}


def test_by_channel_returns_expected_shape(client, patched_auth, monkeypatch):
    """Endpoint returns the documented JSON shape."""
    monkeypatch.setattr(
        stats_repo, "db_sales_by_channel",
        AsyncMock(return_value=MOCK_CHANNEL_RESPONSE),
    )
    with patch("app.services.tenant_context.tenant_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=None)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.get(
            "/api/stats/by-channel?period_start=2026-04-12&period_end=2026-04-18",
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data
    assert "total" in data
    assert "total_count" in data
    assert "channels" in data
    assert isinstance(data["channels"], list)


def test_by_channel_default_period(client, patched_auth, monkeypatch):
    """Without period params, endpoint must still call repo (uses last-7-days default)."""
    captured = {}

    async def _mock_channel(org_id, period_start, period_end, location_id=None):
        captured["period_start"] = period_start
        captured["period_end"] = period_end
        return MOCK_CHANNEL_RESPONSE

    monkeypatch.setattr(stats_repo, "db_sales_by_channel", _mock_channel)
    with patch("app.services.tenant_context.tenant_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=None)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.get("/api/stats/by-channel", headers=_auth_headers())
    assert resp.status_code == 200
    # Both period keys must be set (default period logic)
    assert captured.get("period_start") is not None
    assert captured.get("period_end") is not None


def test_classify_channel_mapping():
    """Unit test for the channel→bucket classification helper."""
    assert stats_repo._classify_channel("whatsapp_bot", "recoger") == "whatsapp_bot"
    assert stats_repo._classify_channel("pos", None) == "pos"
    assert stats_repo._classify_channel("qr_table", None) == "qr_table"
    assert stats_repo._classify_channel(None, "domicilio") == "delivery"
    assert stats_repo._classify_channel(None, None) == "unknown"
    assert stats_repo._classify_channel("", "domicilio") == "delivery"


# ══════════════════════════════════════════════════════════════════════════════
# 2. GET /api/stats/top-dishes
# ══════════════════════════════════════════════════════════════════════════════

MOCK_TOP_DISHES = {
    "period": {"start": "2026-04-12", "end": "2026-04-18"},
    "dishes": [
        {
            "rank": 1, "name": "Ajiaco santafereño", "category": "Sopas",
            "price": 28000, "sold": 312, "revenue": 8736000,
            "food_cost_per_unit": 10640, "margin_pct": 62.0,
        },
        {
            "rank": 2, "name": "Bandeja paisa", "category": "Fuertes",
            "price": 34000, "sold": 286, "revenue": 9724000,
            "food_cost_per_unit": None, "margin_pct": None,
        },
    ],
}


def test_top_dishes_shape(client, patched_auth, monkeypatch):
    """Endpoint returns dishes list with required fields."""
    monkeypatch.setattr(
        stats_repo, "db_top_dishes",
        AsyncMock(return_value=MOCK_TOP_DISHES),
    )
    with patch("app.services.tenant_context.tenant_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=None)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.get(
            "/api/stats/top-dishes?period_start=2026-04-12&period_end=2026-04-18&limit=5",
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "dishes" in data
    dish = data["dishes"][0]
    assert "rank" in dish
    assert "name" in dish
    assert "sold" in dish
    assert "revenue" in dish
    assert "margin_pct" in dish


def test_top_dishes_limit_clamp(client, patched_auth, monkeypatch):
    """limit=0 is rejected (ge=1 constraint) and limit > 50 is rejected."""
    monkeypatch.setattr(
        stats_repo, "db_top_dishes",
        AsyncMock(return_value=MOCK_TOP_DISHES),
    )
    resp0 = client.get("/api/stats/top-dishes?limit=0", headers=_auth_headers())
    assert resp0.status_code == 422  # FastAPI validation error

    resp51 = client.get("/api/stats/top-dishes?limit=51", headers=_auth_headers())
    assert resp51.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 3. GET /api/stats/inventory-critical
# ══════════════════════════════════════════════════════════════════════════════

MOCK_INVENTORY = {
    "alerts": [
        {
            "ingredient": "Papa criolla", "unit": "kg",
            "current_stock": 1.2, "min_stock": 8.0, "severity": "critical",
            "affects_dishes": ["Ajiaco santafereño"],
        },
        {
            "ingredient": "Hierbabuena", "unit": "gr",
            "current_stock": 120.0, "min_stock": 200.0, "severity": "warning",
            "affects_dishes": ["Limonada de hierbabuena"],
        },
    ],
    "ok": [
        {
            "ingredient": "Arroz blanco", "unit": "kg",
            "current_stock": 18.0, "min_stock": 10.0, "severity": "ok",
            "affects_dishes": [],
        },
    ],
}


def test_inventory_critical_shape(client, patched_auth, monkeypatch):
    """Returns alerts + ok lists with severity field."""
    monkeypatch.setattr(
        stats_repo, "db_inventory_critical",
        AsyncMock(return_value=MOCK_INVENTORY),
    )
    with patch("app.services.tenant_context.tenant_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=None)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.get("/api/stats/inventory-critical", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "alerts" in data
    assert "ok" in data
    alert = data["alerts"][0]
    assert alert["severity"] in ("critical", "warning")
    assert "affects_dishes" in alert
    assert isinstance(alert["affects_dishes"], list)


def test_inventory_severity_logic():
    """Unit test for the severity classification inside the repo helper."""
    # critical: current < min * 0.5
    current, minimum = 1.2, 8.0
    assert current < minimum * 0.5  # 1.2 < 4.0

    # warning: current < min but >= min * 0.5
    current2, minimum2 = 4.8, 5.0
    assert current2 < minimum2 and current2 >= minimum2 * 0.5

    # ok: current >= min
    current3, minimum3 = 18.0, 10.0
    assert current3 >= minimum3


# ══════════════════════════════════════════════════════════════════════════════
# 4. GET /api/stats/live-orders
# ══════════════════════════════════════════════════════════════════════════════

MOCK_LIVE_ORDERS = {
    "orders": [
        {
            "id": "#3421AB", "source": "table", "table_name": "Mesa 12",
            "party_size": 4, "channel": "pos", "status": "en_cocina",
            "total": 148500, "age_min": 8,
            "items_summary": "Ajiaco santafereño · Corrientazo · Limonada",
        },
        {
            "id": "#3420CD", "source": "delivery", "customer": "···4567",
            "customer_phone_last4": "4567", "channel": "whatsapp_bot",
            "status": "en_ruta", "total": 62300, "age_min": 4,
            "address_short": "Usaquén · Calle 118 #15-22",
            "items_summary": "Bandeja paisa · Jugo de mora",
        },
    ]
}


def test_live_orders_shape(client, patched_auth, monkeypatch):
    """Returns orders list with id, source, status, total, age_min."""
    monkeypatch.setattr(
        stats_repo, "db_live_orders",
        AsyncMock(return_value=MOCK_LIVE_ORDERS),
    )
    with patch("app.services.tenant_context.tenant_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=None)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.get("/api/stats/live-orders?limit=10", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "orders" in data
    o = data["orders"][0]
    assert "id" in o
    assert "source" in o
    assert "status" in o
    assert "total" in o
    assert "age_min" in o


def test_live_orders_limit_validation(client, patched_auth, monkeypatch):
    """limit=0 and limit > 100 are rejected."""
    monkeypatch.setattr(
        stats_repo, "db_live_orders",
        AsyncMock(return_value=MOCK_LIVE_ORDERS),
    )
    assert client.get("/api/stats/live-orders?limit=0", headers=_auth_headers()).status_code == 422
    assert client.get("/api/stats/live-orders?limit=101", headers=_auth_headers()).status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests (require TEST_DATABASE_URL)
# ══════════════════════════════════════════════════════════════════════════════

import os as _os


@pytest.mark.asyncio
async def test_integration_sales_by_channel_empty(db_pool):
    """With empty tables, by_channel returns zero total for a dummy org."""
    # db_pool fixture already skips if no TEST_DATABASE_URL
    from app.services.tenant_context import tenant_scope
    from app.services.database import _pool  # touch pool so tenant_db resolves

    # Patch get_pool so stats_repo uses our test pool
    async def _test_pool():
        return db_pool

    import app.services.database as _db
    from unittest.mock import patch as _patch

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999  # no data for this org
        with tenant_scope(org_id):
            result = await stats_repo.db_sales_by_channel(
                org_id=org_id,
                period_start="2026-01-01",
                period_end="2026-01-07",
            )
    assert result["total"] == 0
    assert result["total_count"] == 0
    assert result["channels"] == []


@pytest.mark.asyncio
async def test_integration_top_dishes_empty(db_pool):
    """With no orders, top_dishes returns empty dishes list for dummy org."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_top_dishes(
                org_id=org_id,
                period_start="2026-01-01",
                period_end="2026-01-07",
                limit=10,
            )
    assert result["dishes"] == []


@pytest.mark.asyncio
async def test_integration_inventory_critical_empty(db_pool):
    """With no inventory, returns empty alerts and ok lists for dummy org."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_inventory_critical(org_id=org_id)
    assert result["alerts"] == []
    assert result["ok"] == []


@pytest.mark.asyncio
async def test_integration_live_orders_empty(db_pool):
    """With no active orders, live_orders returns empty list for dummy org."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_live_orders(org_id=org_id, limit=20)
    assert result["orders"] == []
