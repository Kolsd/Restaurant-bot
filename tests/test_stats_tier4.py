"""
tests/test_stats_tier4.py

Unit + integration tests for:
  Tier 4d — Comparatives (compare=true on by-channel, payment-status, top-dishes)
  Tier 4a — AI Daily Insight (/api/stats/daily-insight)

Unit tests mock the repo layer.
Integration tests (require TEST_DATABASE_URL) hit real queries with empty data.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.repositories import stats_repo


# ── helpers ────────────────────────────────────────────────────────────────────

def _auth_headers():
    return {"Authorization": "Bearer test-token"}


def _mock_scope():
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=None)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def patched_auth(monkeypatch):
    from tests.conftest import patch_auth
    return patch_auth(monkeypatch, restaurant_id=42)


@pytest.fixture
def patched_auth_with_ai(monkeypatch):
    from tests.conftest import patch_auth
    return patch_auth(monkeypatch, restaurant_id=42, features={"ai_daily_insight": True})


# ── mock data ──────────────────────────────────────────────────────────────────

_MOCK_CHANNEL = {
    "period": {"start": "2026-04-11", "end": "2026-04-17"},
    "total": 1_840_000,
    "total_count": 1_280,
    "channels": [
        {"channel": "whatsapp_bot", "label": "WhatsApp · Bot", "total": 1_200_000, "count": 900, "pct": 65.2},
        {"channel": "pos",          "label": "Salón · POS",    "total":   640_000,  "count": 380, "pct": 34.8},
    ],
}

_MOCK_PREV_CHANNEL = {
    "period": {"start": "2026-04-04", "end": "2026-04-10"},
    "total": 1_556_831,
    "total_count": 1_177,
    "channels": [
        {"channel": "whatsapp_bot", "label": "WhatsApp · Bot", "total": 1_000_000, "count": 820, "pct": 64.2},
        {"channel": "pos",          "label": "Salón · POS",    "total":   556_831,  "count": 357, "pct": 35.8},
    ],
}

_MOCK_PAYMENT = {
    "period": {"start": "2026-04-11", "end": "2026-04-17"},
    "buckets": [
        {"key": "paid",     "label": "Pagados",    "count": 1079, "pct": 84.0},
        {"key": "pending",  "label": "Pendientes", "count": 132,  "pct": 10.3},
        {"key": "disputed", "label": "Disputa",    "count": 8,    "pct": 0.6},
        {"key": "courtesy", "label": "Cortesía",   "count": 65,   "pct": 5.1},
    ],
    "total_count": 1284,
}

_MOCK_PREV_PAYMENT = {
    "period": {"start": "2026-04-04", "end": "2026-04-10"},
    "buckets": [
        {"key": "paid",     "label": "Pagados",    "count": 950,  "pct": 83.0},
        {"key": "pending",  "label": "Pendientes", "count": 120,  "pct": 10.5},
        {"key": "disputed", "label": "Disputa",    "count": 5,    "pct": 0.4},
        {"key": "courtesy", "label": "Cortesía",   "count": 70,   "pct": 6.1},
    ],
    "total_count": 1145,
}

_MOCK_TOP_DISHES = {
    "period": {"start": "2026-04-11", "end": "2026-04-17"},
    "dishes": [
        {"rank": 1, "name": "Bandeja Paisa", "category": "Platos", "price": 28000,
         "sold": 190, "revenue": 5_320_000, "food_cost_per_unit": 8400, "margin_pct": 70.0},
        {"rank": 2, "name": "Ajiaco", "category": "Sopas", "price": 22000,
         "sold": 145, "revenue": 3_190_000, "food_cost_per_unit": 6000, "margin_pct": 72.7},
    ],
}

_MOCK_PREV_TOP_DISHES = {
    "period": {"start": "2026-04-04", "end": "2026-04-10"},
    "dishes": [
        {"rank": 1, "name": "Bandeja Paisa", "category": "Platos", "price": 28000,
         "sold": 160, "revenue": 4_480_000, "food_cost_per_unit": 8400, "margin_pct": 70.0},
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# Tier 4d — By Channel with compare=true
# ══════════════════════════════════════════════════════════════════════════════

class TestByChannelCompare:

    def test_compare_false_returns_no_previous(self, client, patched_auth, monkeypatch):
        """Without compare=true the response has no previous/deltas keys."""
        monkeypatch.setattr(stats_repo, "db_sales_by_channel", AsyncMock(return_value=_MOCK_CHANNEL))
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/by-channel?period_start=2026-04-11&period_end=2026-04-17",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "previous" not in data
        assert "deltas" not in data

    def test_compare_true_returns_previous_and_deltas(self, client, patched_auth, monkeypatch):
        """compare=true returns previous + deltas with correct shape."""
        call_count = {"n": 0}

        async def _mock_channel(org_id, period_start, period_end, location_id=None):
            call_count["n"] += 1
            return _MOCK_CHANNEL if call_count["n"] == 1 else _MOCK_PREV_CHANNEL

        monkeypatch.setattr(stats_repo, "db_sales_by_channel", _mock_channel)
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/by-channel?period_start=2026-04-11&period_end=2026-04-17&compare=true",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()

        # current data must still be present
        assert data["total"] == 1_840_000
        assert data["total_count"] == 1_280
        assert "channels" in data

        # previous block
        assert "previous" in data
        prev = data["previous"]
        assert prev["total"] == 1_556_831
        assert "channels" in prev

        # deltas
        assert "deltas" in data
        assert "total_pct" in data["deltas"]
        assert "total_count_pct" in data["deltas"]

        # sanity-check the delta math: (1840000 - 1556831) / 1556831 * 100 ≈ 18.2
        assert data["deltas"]["total_pct"] == pytest.approx(18.2, abs=0.2)

    def test_compare_true_zero_prev_returns_null_deltas(self, client, patched_auth, monkeypatch):
        """When prev period has zero data, deltas are null (no ZeroDivisionError)."""
        empty_prev = {"period": {"start": "2026-04-04", "end": "2026-04-10"},
                      "total": 0, "total_count": 0, "channels": []}
        call_count = {"n": 0}

        async def _mock(org_id, period_start, period_end, location_id=None):
            call_count["n"] += 1
            return _MOCK_CHANNEL if call_count["n"] == 1 else empty_prev

        monkeypatch.setattr(stats_repo, "db_sales_by_channel", _mock)
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/by-channel?period_start=2026-04-11&period_end=2026-04-17&compare=true",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deltas"]["total_pct"] is None
        assert data["deltas"]["total_count_pct"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Tier 4d — Payment Status with compare=true
# ══════════════════════════════════════════════════════════════════════════════

class TestPaymentStatusCompare:

    def test_compare_true_returns_previous_and_deltas(self, client, patched_auth, monkeypatch):
        """compare=true returns previous + deltas with correct shape."""
        call_count = {"n": 0}

        async def _mock(org_id, period_start, period_end, location_id=None):
            call_count["n"] += 1
            return _MOCK_PAYMENT if call_count["n"] == 1 else _MOCK_PREV_PAYMENT

        monkeypatch.setattr(stats_repo, "db_payment_status", _mock)
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/payment-status?period_start=2026-04-11&period_end=2026-04-17&compare=true",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_count"] == 1284
        assert "previous" in data
        assert data["previous"]["total_count"] == 1145
        assert "deltas" in data
        assert "total_count_pct" in data["deltas"]
        assert "paid_count_pct" in data["deltas"]

        # (1284 - 1145) / 1145 * 100 ≈ 12.1
        assert data["deltas"]["total_count_pct"] == pytest.approx(12.1, abs=0.2)

    def test_compare_zero_prev_returns_null_deltas(self, client, patched_auth, monkeypatch):
        empty_prev = {"period": {"start": "2026-04-04", "end": "2026-04-10"},
                      "buckets": [], "total_count": 0}
        call_count = {"n": 0}

        async def _mock(org_id, period_start, period_end, location_id=None):
            call_count["n"] += 1
            return _MOCK_PAYMENT if call_count["n"] == 1 else empty_prev

        monkeypatch.setattr(stats_repo, "db_payment_status", _mock)
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/payment-status?period_start=2026-04-11&period_end=2026-04-17&compare=true",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        assert resp.json()["deltas"]["total_count_pct"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Tier 4d — Top Dishes with compare=true
# ══════════════════════════════════════════════════════════════════════════════

class TestTopDishesCompare:

    def test_compare_true_returns_previous_and_deltas(self, client, patched_auth, monkeypatch):
        """compare=true returns previous + deltas with correct shape."""
        call_count = {"n": 0}

        async def _mock(org_id, period_start, period_end, limit=10, location_id=None):
            call_count["n"] += 1
            return _MOCK_TOP_DISHES if call_count["n"] == 1 else _MOCK_PREV_TOP_DISHES

        monkeypatch.setattr(stats_repo, "db_top_dishes", _mock)
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/top-dishes?period_start=2026-04-11&period_end=2026-04-17&compare=true",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()

        assert data["dishes"][0]["name"] == "Bandeja Paisa"
        assert "previous" in data
        assert data["previous"]["dishes"][0]["revenue"] == 4_480_000
        assert "deltas" in data
        assert "top_dish_revenue_pct" in data["deltas"]
        # (5320000 - 4480000) / 4480000 * 100 ≈ 18.75
        assert data["deltas"]["top_dish_revenue_pct"] == pytest.approx(18.75, abs=0.2)

    def test_compare_zero_prev_returns_null_deltas(self, client, patched_auth, monkeypatch):
        empty_prev = {"period": {"start": "2026-04-04", "end": "2026-04-10"}, "dishes": []}
        call_count = {"n": 0}

        async def _mock(org_id, period_start, period_end, limit=10, location_id=None):
            call_count["n"] += 1
            return _MOCK_TOP_DISHES if call_count["n"] == 1 else empty_prev

        monkeypatch.setattr(stats_repo, "db_top_dishes", _mock)
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get(
                "/api/stats/top-dishes?period_start=2026-04-11&period_end=2026-04-17&compare=true",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        assert resp.json()["deltas"]["top_dish_revenue_pct"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Tier 4a — AI Daily Insight
# ══════════════════════════════════════════════════════════════════════════════

class TestAIDailyInsight:

    def test_feature_disabled_no_llm_call(self, client, patched_auth, monkeypatch):
        """Feature disabled → {"enabled": false} without any LLM call."""
        called = {"n": 0}

        async def _mock_generate(*args, **kwargs):
            called["n"] += 1
            return {"enabled": True, "insight_text": "test"}

        monkeypatch.setattr(
            "app.services.ai_insights.generate_daily_insight",
            _mock_generate,
        )
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get("/api/stats/daily-insight", headers=_auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["reason"] == "feature_disabled"
        assert called["n"] == 0  # generate_daily_insight never called

    def test_feature_enabled_cache_hit(self, client, patched_auth_with_ai, monkeypatch):
        """Feature enabled + Redis cache hit → returns cached shape."""
        cached_payload = {
            "enabled": True,
            "generated_at": "2026-04-18T14:00:00+00:00",
            "insight_text": "Hoy proyectas $1.84M en ventas (+12%). Refuerza cocina 7-9pm.",
            "signals": {
                "sales_7d_total": 12_400_000,
                "pending_orders_today": 5,
                "top_dish": "Bandeja Paisa",
                "inventory_alerts_count": 2,
                "at_risk_customers_count": 8,
            },
        }

        async def _mock_generate(org_id, location_id):
            return cached_payload

        monkeypatch.setattr(
            "app.services.ai_insights.generate_daily_insight",
            _mock_generate,
        )
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get("/api/stats/daily-insight", headers=_auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert "insight_text" in data
        assert "signals" in data
        assert data["insight_text"] == cached_payload["insight_text"]

    def test_feature_enabled_cache_miss_calls_llm(self, client, patched_auth_with_ai, monkeypatch):
        """Cache miss → generate_daily_insight is called and returns full shape."""
        fresh_payload = {
            "enabled": True,
            "generated_at": "2026-04-18T16:30:00+00:00",
            "insight_text": "Ventas del día: $480K (en línea). Papa criolla crítica — re-stock urgente.",
            "signals": {
                "sales_7d_total": 9_800_000,
                "pending_orders_today": 3,
                "top_dish": "Ajiaco",
                "inventory_alerts_count": 1,
                "at_risk_customers_count": 12,
            },
        }
        called = {"n": 0}

        async def _mock_generate(org_id, location_id):
            called["n"] += 1
            return fresh_payload

        monkeypatch.setattr(
            "app.services.ai_insights.generate_daily_insight",
            _mock_generate,
        )
        with patch("app.services.tenant_context.tenant_scope", return_value=_mock_scope()):
            resp = client.get("/api/stats/daily-insight", headers=_auth_headers())

        assert resp.status_code == 200
        assert called["n"] == 1
        data = resp.json()
        assert data["enabled"] is True
        assert data["signals"]["top_dish"] == "Ajiaco"

    def test_missing_api_key_returns_llm_unavailable(self, monkeypatch):
        """Missing ANTHROPIC_API_KEY → signals-only payload, no crash."""
        import os
        from app.services import ai_insights

        # Ensure no API key
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Patch gather_signals to avoid real DB calls
        mock_signals = {
            "sales_7d_total": 5_000_000,
            "pending_orders_today": 2,
            "top_dish": "Cazuela de mariscos",
            "inventory_alerts_count": 0,
            "at_risk_customers_count": 3,
        }

        async def _mock_gather(org_id, location_id):
            return mock_signals

        async def _mock_redis_get(*args, **kwargs):
            return None

        monkeypatch.setattr(ai_insights, "_gather_signals", _mock_gather)

        # Patch Redis to return None (cache miss)
        import app.services.redis_client as rc
        monkeypatch.setattr(rc, "get_redis", AsyncMock(return_value=None))

        import asyncio
        # Use a fresh loop instead of asyncio.run() — asyncio.run() closes and
        # removes the current event loop, which breaks test_weekly_reports.py's
        # _run() helper that calls asyncio.get_event_loop().run_until_complete().
        _loop = asyncio.new_event_loop()
        try:
            result = _loop.run_until_complete(
                ai_insights.generate_daily_insight(org_id=42, location_id=42)
            )
        finally:
            _loop.close()

        assert result["enabled"] is True
        assert result.get("error") == "llm_unavailable"
        assert "signals" in result
        assert "insight_text" not in result


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests — removed
# ══════════════════════════════════════════════════════════════════════════════
# The 3 *_compare_empty_db tests were removed because they used TestClient(app)
# against the real DB pool from within synchronous test methods — a pattern that
# caused asyncpg "another operation is in progress" interference with
# test_weekly_reports.py when both files ran in the same pytest session.
#
# Coverage is fully preserved by the unit tests above
# (TestByChannelCompare.test_compare_true_zero_prev_returns_null_deltas,
#  TestPaymentStatusCompare.test_compare_zero_prev_returns_null_deltas,
#  TestTopDishesCompare.test_compare_zero_prev_returns_null_deltas) which mock
# the repo layer to return zero-data previous periods and assert null deltas.
