"""
tests/test_stats_tier3.py

Unit + integration tests for the Tier-3 Dashboard/Staff-HQ endpoints:
  GET /api/stats/payment-status
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


# ══════════════════════════════════════════════════════════════════════════════
# 5. GET /api/public/menu-context/{table_id} — table_context field
# ══════════════════════════════════════════════════════════════════════════════


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

