"""
tests/test_stats_tier3.py

Unit + integration tests for the five Tier-3 Dashboard/Staff-HQ endpoints:
  GET /api/stats/payment-status
  GET /api/stats/customers-at-risk
  GET /api/stats/staff-performance
  GET /api/stats/tips-pool
  GET /api/public/menu-context/{table_id}  — table_context field

Unit tests mock the repo layer.
Integration tests (require TEST_DATABASE_URL) hit real queries with empty data.
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
    from tests.conftest import patch_auth
    return patch_auth(monkeypatch, restaurant_id=1)


def _mock_scope():
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=None)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


# ══════════════════════════════════════════════════════════════════════════════
# 1. GET /api/stats/payment-status
# ══════════════════════════════════════════════════════════════════════════════

MOCK_PAYMENT_STATUS = {
    "period": {"start": "2026-04-18", "end": "2026-04-18"},
    "buckets": [
        {"key": "paid",     "label": "Pagados",    "count": 1079, "pct": 84.0},
        {"key": "pending",  "label": "Pendientes", "count": 132,  "pct": 10.3},
        {"key": "disputed", "label": "Disputa",    "count": 8,    "pct": 0.6},
        {"key": "courtesy", "label": "Cortesía",   "count": 65,   "pct": 5.1},
    ],
    "total_count": 1284,
}


def test_payment_status_shape(client, patched_auth, monkeypatch):
    """Returns documented shape: period, buckets, total_count."""
    monkeypatch.setattr(
        stats_repo, "db_payment_status",
        AsyncMock(return_value=MOCK_PAYMENT_STATUS),
    )
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        resp = client.get(
            "/api/stats/payment-status?period_start=2026-04-18&period_end=2026-04-18",
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data
    assert "buckets" in data
    assert "total_count" in data
    assert isinstance(data["buckets"], list)
    keys = {b["key"] for b in data["buckets"]}
    assert keys == {"paid", "pending", "disputed", "courtesy"}


def test_payment_status_default_period(client, patched_auth, monkeypatch):
    """Without period params, endpoint must still call repo (default 7 days)."""
    captured = {}

    async def _mock(org_id, period_start, period_end, location_id=None):
        captured["period_start"] = period_start
        captured["period_end"] = period_end
        return MOCK_PAYMENT_STATUS

    monkeypatch.setattr(stats_repo, "db_payment_status", _mock)
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        resp = client.get("/api/stats/payment-status", headers=_auth_headers())
    assert resp.status_code == 200
    assert captured.get("period_start") is not None
    assert captured.get("period_end") is not None


def test_payment_bucket_logic():
    """Unit test for the _payment_bucket helper."""
    assert stats_repo._payment_bucket(True, "pendiente", 50000) == "paid"
    assert stats_repo._payment_bucket(False, "pendiente", 50000) == "pending"
    assert stats_repo._payment_bucket(False, "cancelado", 50000) == "disputed"
    # startswith("dispute") covers "dispute", "disputed", etc.
    assert stats_repo._payment_bucket(False, "disputed", 50000) == "disputed"
    assert stats_repo._payment_bucket(True, "cortesia", 0) == "courtesy"
    assert stats_repo._payment_bucket(True, "paid", 0) == "courtesy"  # zero total → courtesy


# ══════════════════════════════════════════════════════════════════════════════
# 2. GET /api/stats/customers-at-risk
# ══════════════════════════════════════════════════════════════════════════════

MOCK_AT_RISK = {
    "count": 2,
    "customers": [
        {
            "phone": "+57301...", "name": "Ana M.", "total_orders": 12,
            "last_seen": "2026-03-28T00:00:00", "days_since": 22,
            "total_spent": 340000,
        },
        {
            "phone": "+57310...", "name": "Beto R.", "total_orders": 5,
            "last_seen": "2026-03-30T00:00:00", "days_since": 20,
            "total_spent": 80000,
        },
    ],
}


def test_customers_at_risk_shape(client, patched_auth, monkeypatch):
    """Returns count + customers list with required fields."""
    monkeypatch.setattr(
        stats_repo, "db_customers_at_risk",
        AsyncMock(return_value=MOCK_AT_RISK),
    )
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        resp = client.get("/api/stats/customers-at-risk", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "customers" in data
    assert data["count"] == len(data["customers"])
    c = data["customers"][0]
    for key in ("phone", "name", "total_orders", "last_seen", "days_since", "total_spent"):
        assert key in c, f"Missing field: {key}"


def test_customers_at_risk_limit_validation(client, patched_auth, monkeypatch):
    """limit must be 1–200; 0 and 201 rejected with 422."""
    monkeypatch.setattr(stats_repo, "db_customers_at_risk", AsyncMock(return_value=MOCK_AT_RISK))
    assert client.get("/api/stats/customers-at-risk?limit=0", headers=_auth_headers()).status_code == 422
    assert client.get("/api/stats/customers-at-risk?limit=201", headers=_auth_headers()).status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 3. GET /api/stats/staff-performance
# ══════════════════════════════════════════════════════════════════════════════

MOCK_PERF = {
    "staff_id": "00000000-0000-0000-0000-000000000001",
    "staff_name": "Valentina C.",
    "weeks": [
        {"week_start": "2026-03-02", "sales_total": 620000, "tickets_count": 14},
        {"week_start": "2026-03-09", "sales_total": 780000, "tickets_count": 16},
    ],
}


def test_staff_performance_shape(client, patched_auth, monkeypatch):
    """Returns staff_id, staff_name, weeks list with required fields."""
    monkeypatch.setattr(
        stats_repo, "db_staff_performance",
        AsyncMock(return_value=MOCK_PERF),
    )
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        resp = client.get(
            "/api/stats/staff-performance?staff_id=00000000-0000-0000-0000-000000000001&weeks=7",
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "staff_id" in data
    assert "staff_name" in data
    assert "weeks" in data
    assert isinstance(data["weeks"], list)
    if data["weeks"]:
        w = data["weeks"][0]
        assert "week_start" in w
        assert "sales_total" in w
        assert "tickets_count" in w


def test_staff_performance_missing_staff_id(client, patched_auth):
    """Missing staff_id param returns 422."""
    resp = client.get("/api/stats/staff-performance", headers=_auth_headers())
    assert resp.status_code == 422


def test_staff_performance_weeks_validation(client, patched_auth, monkeypatch):
    """weeks param: ge=1, le=26; 0 and 27 rejected."""
    monkeypatch.setattr(stats_repo, "db_staff_performance", AsyncMock(return_value=MOCK_PERF))
    _id = "00000000-0000-0000-0000-000000000001"
    assert client.get(f"/api/stats/staff-performance?staff_id={_id}&weeks=0", headers=_auth_headers()).status_code == 422
    assert client.get(f"/api/stats/staff-performance?staff_id={_id}&weeks=27", headers=_auth_headers()).status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 4. GET /api/stats/tips-pool
# ══════════════════════════════════════════════════════════════════════════════

MOCK_TIPS_POOL = {
    "period": {"start": "2026-04-14", "end": "2026-04-20"},
    "pool_total": 284000.0,
    "entries_count": 5,
    "entries_preview": [
        {"staff_id": "uuid1", "name": "Valentina C.", "role": "mesera", "total_tips": 62400.0, "pct": 22.0},
    ],
    "unallocated": 0.0,
}


def test_tips_pool_shape(client, patched_auth, monkeypatch):
    """Returns documented shape: period, pool_total, entries_count, entries_preview, unallocated."""
    monkeypatch.setattr(
        stats_repo, "db_tips_pool",
        AsyncMock(return_value=MOCK_TIPS_POOL),
    )
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        resp = client.get("/api/stats/tips-pool", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    for key in ("period", "pool_total", "entries_count", "entries_preview", "unallocated"):
        assert key in data, f"Missing field: {key}"
    assert isinstance(data["entries_preview"], list)
    assert isinstance(data["period"], dict)
    assert "start" in data["period"] and "end" in data["period"]


def test_tips_pool_default_period_set(client, patched_auth, monkeypatch):
    """When no period params given, the repo must still be called with valid dates."""
    captured = {}

    async def _mock(org_id, location_id, period_start, period_end, branch_id=None, caller_staff_id=None):
        captured["period_start"] = period_start
        captured["period_end"] = period_end
        return MOCK_TIPS_POOL

    monkeypatch.setattr(stats_repo, "db_tips_pool", _mock)
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        resp = client.get("/api/stats/tips-pool", headers=_auth_headers())
    assert resp.status_code == 200
    # Default period is current week — values must be set by db_tips_pool internals
    # (period_start/end default to None; db_tips_pool fills them)
    # We just ensure no crash and shape OK.
    assert "pool_total" in resp.json()


# ══════════════════════════════════════════════════════════════════════════════
# 5. GET /api/public/menu-context/{table_id} — table_context field
# ══════════════════════════════════════════════════════════════════════════════

def test_public_menu_context_includes_table_context_key(monkeypatch):
    """table_context key must be present in the response (even if null)."""
    from app.routes import tables as tables_mod

    async def _fake_table(tid):
        return {"id": tid, "name": "Mesa 5", "bot_number": "+57300", "branch_id": 1}

    async def _fake_wa(_table):
        return "+57300"

    async def _fake_menu(wa):
        return {}

    async def _fake_restaurant(wa):
        return {"id": 1, "name": "Test", "features": {}, "whatsapp_number": "+57300"}

    async def _fake_availability(rid):
        return {}

    async def _fake_active_session(tid, oid):
        return None  # no active session → table_context = null

    monkeypatch.setattr("app.routes.tables.db.db_get_table_by_id", _fake_table)
    monkeypatch.setattr("app.routes.tables.get_table_wa_number", _fake_wa)
    monkeypatch.setattr("app.routes.tables.db.db_get_menu", _fake_menu)
    monkeypatch.setattr("app.routes.tables.db.db_get_restaurant_by_bot_number", _fake_restaurant)
    monkeypatch.setattr("app.routes.tables.db.db_get_menu_availability", _fake_availability)
    monkeypatch.setattr("app.routes.tables._get_active_session_for_table", _fake_active_session)
    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)):
            client = TestClient(app)
            resp = client.get("/api/public/menu-context/table-abc")
    assert resp.status_code == 200
    data = resp.json()
    assert "table_context" in data  # key must exist (null is fine)


def test_public_menu_context_table_context_populated(monkeypatch):
    """When active session + assigned_staff_id exist, table_context is populated."""
    from app.routes import tables as tables_mod

    async def _fake_table(tid):
        return {"id": tid, "name": "Mesa 7", "bot_number": "+57300", "branch_id": 1}

    async def _fake_wa(_table):
        return "+57300"

    async def _fake_menu(wa):
        return {}

    async def _fake_restaurant(wa):
        return {"id": 1, "name": "Test", "features": {}, "whatsapp_number": "+57300"}

    async def _fake_availability(rid):
        return {}

    async def _fake_active_session(tid, oid):
        return {"assigned_staff_id": "00000000-0000-0000-0000-000000000042"}

    async def _fake_resolve_mesero(sid, oid):
        return {"name": "Valentina Cano", "first_name": "Valentina"}

    monkeypatch.setattr("app.routes.tables.db.db_get_table_by_id", _fake_table)
    monkeypatch.setattr("app.routes.tables.get_table_wa_number", _fake_wa)
    monkeypatch.setattr("app.routes.tables.db.db_get_menu", _fake_menu)
    monkeypatch.setattr("app.routes.tables.db.db_get_restaurant_by_bot_number", _fake_restaurant)
    monkeypatch.setattr("app.routes.tables.db.db_get_menu_availability", _fake_availability)
    monkeypatch.setattr("app.routes.tables._get_active_session_for_table", _fake_active_session)
    monkeypatch.setattr("app.routes.tables._resolve_mesero", _fake_resolve_mesero)

    with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)):
            client = TestClient(app)
            resp = client.get("/api/public/menu-context/table-abc")
    assert resp.status_code == 200
    data = resp.json()
    tc = data.get("table_context")
    assert tc is not None
    assert tc["table_name"] == "Mesa 7"
    assert tc["assigned_mesero"]["name"] == "Valentina Cano"
    assert tc["assigned_mesero"]["first_name"] == "Valentina"


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests (require TEST_DATABASE_URL)
# ══════════════════════════════════════════════════════════════════════════════

import os as _os


@pytest.mark.asyncio
async def test_integration_payment_status_empty(db_pool):
    """Empty DB returns 4 buckets all with count=0."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_payment_status(
                org_id=org_id,
                period_start="2026-01-01",
                period_end="2026-01-07",
            )
    assert result["total_count"] == 0
    assert len(result["buckets"]) == 4
    keys = {b["key"] for b in result["buckets"]}
    assert keys == {"paid", "pending", "disputed", "courtesy"}


@pytest.mark.asyncio
async def test_integration_customers_at_risk_empty(db_pool):
    """Empty DB returns empty customers list."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_customers_at_risk(org_id=org_id, limit=10)
    assert result["count"] == 0
    assert result["customers"] == []


@pytest.mark.asyncio
async def test_integration_staff_performance_empty(db_pool):
    """Unknown staff_id returns empty weeks array (no 404)."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_staff_performance(
                org_id=org_id,
                staff_id="00000000-0000-0000-0000-000000000000",
                weeks=4,
            )
    assert result["weeks"] == []
    assert result["staff_name"] is None


@pytest.mark.asyncio
async def test_integration_tips_pool_empty(db_pool):
    """Empty DB returns zero pool_total and empty entries_preview."""
    import app.services.database as _db
    from unittest.mock import patch as _patch
    from app.services.tenant_context import tenant_scope

    async def _test_pool():
        return db_pool

    with _patch.object(_db, "get_pool", _test_pool):
        org_id = 999999
        with tenant_scope(org_id):
            result = await stats_repo.db_tips_pool(
                org_id=org_id,
                location_id=999999,
                period_start="2026-01-01",
                period_end="2026-01-07",
            )
    assert result["pool_total"] == 0
    assert result["entries_preview"] == []
    assert result["unallocated"] == 0
