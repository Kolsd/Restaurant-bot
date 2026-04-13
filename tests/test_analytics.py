"""
tests/test_analytics.py

Unit tests for the analytics routes.
All DB calls are mocked — no real database needed.

Tests:
  1. test_overview_requires_auth        — no auth → 401
  2. test_overview_returns_data         — valid auth → 200, structure verified
  3. test_restaurants_list              — valid auth → 200, onboarding_score calc verified
  4. test_trends_returns_daily_data     — valid auth → 200, date format verified
  5. test_overview_wrong_key            — wrong key → 401
  6. test_overview_missing_env_key      — ADMIN_KEY not set → 401 on any request
  7. test_trends_empty_tables           — DB tables empty → 200 with empty lists
  8. test_restaurants_onboarding_score  — detailed score calculation edge cases
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app

_PATCH_TARGET = "app.routes.analytics.get_pool"

ADMIN_KEY = "test-analytics-key"
AUTH_HEADER = {"Authorization": f"Bearer {ADMIN_KEY}"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn(fetchval_returns: list = None, fetch_returns: list = None):
    """Build a mock connection whose fetchval/fetch return values in sequence."""
    conn = AsyncMock()
    if fetchval_returns is not None:
        conn.fetchval = AsyncMock(side_effect=fetchval_returns)
    if fetch_returns is not None:
        conn.fetch = AsyncMock(side_effect=fetch_returns)
    return conn


def _make_pool(conn):
    """Wrap a mock conn in a minimal async pool context manager."""
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_multi_pool(conn_sequence: list):
    """Return a pool mock that hands out different conns on each acquire()."""
    pools = []
    for conn in conn_sequence:
        acquire_cm = AsyncMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        pool_i = AsyncMock()
        pool_i.acquire = MagicMock(return_value=acquire_cm)
        pools.append(pool_i)
    get_pool_mock = AsyncMock(side_effect=pools)
    return get_pool_mock


def _const_conn(val):
    """Conn whose fetchval always returns val."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=val)
    return conn


def _make_row(d: dict):
    """Minimal asyncpg Record-like object."""
    row = MagicMock()
    row.__getitem__ = lambda s, k: d[k]
    row.get = lambda k, default=None: d.get(k, default)
    row.keys = lambda: d.keys()
    return row


# ── 1. test_overview_requires_auth ───────────────────────────────────────────

class TestOverviewAuth:
    def test_no_auth_returns_401(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        resp = client.get("/api/analytics/overview")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        resp = client.get("/api/analytics/overview",
                          headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    def test_missing_env_key_returns_401(self, client, monkeypatch):
        monkeypatch.delenv("ADMIN_KEY", raising=False)
        resp = client.get("/api/analytics/overview",
                          headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 401


# ── 2. test_overview_returns_data ────────────────────────────────────────────

class TestOverviewData:
    def _build_pool_mock(self):
        """
        overview calls get_pool() ONCE and then calls pool.acquire() 15 times.
        We return a single pool whose acquire() hands out a new conn each call.

        Metrics in order:
          restaurants: total, active_7d, active_30d, new_this_week, new_this_month  (5)
          orders:      today, this_week, this_month, avg_daily_30d                  (4)
          conversations: today, this_week, active_now                               (3)
          billing:     configured_count, invoices_today, invoices_this_month        (3)
        = 15 acquire() calls total.
        """
        vals = [
            45,    # restaurants.total
            32,    # active_7d
            40,    # active_30d
            3,     # new_this_week
            8,     # new_this_month
            156,   # orders.today
            892,   # orders.this_week
            3421,  # orders.this_month
            114.0, # orders.avg_daily_30d
            423,   # conversations.today
            2100,  # conversations.this_week
            12,    # conversations.active_now
            18,    # billing.configured_count
            45,    # billing.invoices_today
            1200,  # billing.invoices_this_month
        ]

        # Build a sequence of acquire context managers, one per value
        acquire_cms = []
        for v in vals:
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=v)
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=conn)
            cm.__aexit__ = AsyncMock(return_value=False)
            acquire_cms.append(cm)

        pool = AsyncMock()
        pool.acquire = MagicMock(side_effect=acquire_cms)
        return AsyncMock(return_value=pool)

    def test_overview_returns_200_with_structure(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/analytics/overview", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert "restaurants" in body
        assert "orders" in body
        assert "conversations" in body
        assert "billing" in body

    def test_overview_restaurants_keys(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/analytics/overview", headers=AUTH_HEADER)
        r = resp.json()["restaurants"]
        assert "total" in r
        assert "active_7d" in r
        assert "active_30d" in r
        assert "new_this_week" in r
        assert "new_this_month" in r

    def test_overview_orders_values(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/analytics/overview", headers=AUTH_HEADER)
        o = resp.json()["orders"]
        assert o["today"] == 156
        assert o["this_week"] == 892
        assert o["this_month"] == 3421
        assert o["avg_daily_30d"] == 114.0

    def test_overview_billing_keys(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/analytics/overview", headers=AUTH_HEADER)
        b = resp.json()["billing"]
        assert "configured_count" in b
        assert "invoices_today" in b
        assert "invoices_this_month" in b

    def test_overview_individual_metric_failure_returns_none(self, client, monkeypatch):
        """If the first fetchval fails, its value is None; others still populate."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)

        # Build 15 acquire CMs: first one errors, rest return a value
        # Order: total(fail), active_7d=32, active_30d=40, new_this_week=3, new_this_month=8,
        #        orders.today=0, this_week=0, this_month=0, avg=0.0,
        #        conv.today=0, conv.week=0, conv.active_now=0,
        #        billing.configured=0, invoices_today=0, invoices_month=0
        ok_vals = [32, 40, 3, 8, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0]

        acquire_cms = []

        # First CM — conn.fetchval raises
        err_conn = AsyncMock()
        err_conn.fetchval = AsyncMock(side_effect=Exception("table missing"))
        err_cm = AsyncMock()
        err_cm.__aenter__ = AsyncMock(return_value=err_conn)
        err_cm.__aexit__ = AsyncMock(return_value=False)
        acquire_cms.append(err_cm)

        # Remaining 14 CMs — each returns a value
        for v in ok_vals:
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=v)
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=conn)
            cm.__aexit__ = AsyncMock(return_value=False)
            acquire_cms.append(cm)

        pool = AsyncMock()
        pool.acquire = MagicMock(side_effect=acquire_cms)
        get_pool_mock = AsyncMock(return_value=pool)

        with patch(_PATCH_TARGET, get_pool_mock):
            resp = client.get("/api/analytics/overview", headers=AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["restaurants"]["total"] is None      # failed
        assert body["restaurants"]["active_7d"] == 32    # subsequent ok


# ── 3. test_restaurants_list ─────────────────────────────────────────────────

class TestRestaurantsList:
    def test_restaurants_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        resp = client.get("/api/analytics/restaurants")
        assert resp.status_code == 401

    def _make_rest_row(self, **kwargs):
        defaults = {
            "id": 1,
            "name": "Test Rest",
            "whatsapp_number": "+573001234567",
            "created_at": "2025-01-15",
            "menu": [{"category": "Pizzas", "items": []}],
            "features": {"billing_provider": "alegra"},
            "orders_30d": 100,
            "conversations_30d": 300,
            "last_order_at": "2026-04-13",
            "last_conversation_at": "2026-04-13",
            "staff_count": 5,
            "has_any_order": 1,
        }
        defaults.update(kwargs)
        return _make_row(defaults)

    def _pool_with_fetch(self, rows):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=rows)
        return AsyncMock(return_value=_make_pool(conn))

    def test_restaurants_returns_list(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        rows = [self._make_rest_row()]
        with patch(_PATCH_TARGET, self._pool_with_fetch(rows)):
            resp = client.get("/api/analytics/restaurants", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert "restaurants" in body
        assert len(body["restaurants"]) == 1

    def test_onboarding_score_full(self, client, monkeypatch):
        """All 5 steps completed → score 100."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        rows = [self._make_rest_row(
            menu=[{"x": 1}],
            features={"billing_provider": "alegra"},
            whatsapp_number="+57300",
            staff_count=3,
            has_any_order=1,
        )]
        with patch(_PATCH_TARGET, self._pool_with_fetch(rows)):
            resp = client.get("/api/analytics/restaurants", headers=AUTH_HEADER)
        score = resp.json()["restaurants"][0]["onboarding_score"]
        assert score == 100

    def test_onboarding_score_no_menu_no_billing(self, client, monkeypatch):
        """Missing menu + billing → 60%."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        rows = [self._make_rest_row(
            menu=None,
            features={},
            whatsapp_number="+57300",
            staff_count=3,
            has_any_order=1,
        )]
        with patch(_PATCH_TARGET, self._pool_with_fetch(rows)):
            resp = client.get("/api/analytics/restaurants", headers=AUTH_HEADER)
        score = resp.json()["restaurants"][0]["onboarding_score"]
        assert score == 60   # 3 of 5 steps: whatsapp + staff + orders

    def test_onboarding_score_zero(self, client, monkeypatch):
        """Nothing set → 0%."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        rows = [self._make_rest_row(
            menu=None,
            features={},
            whatsapp_number=None,
            staff_count=0,
            has_any_order=0,
        )]
        with patch(_PATCH_TARGET, self._pool_with_fetch(rows)):
            resp = client.get("/api/analytics/restaurants", headers=AUTH_HEADER)
        score = resp.json()["restaurants"][0]["onboarding_score"]
        assert score == 0

    def test_restaurants_row_keys(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        rows = [self._make_rest_row()]
        with patch(_PATCH_TARGET, self._pool_with_fetch(rows)):
            resp = client.get("/api/analytics/restaurants", headers=AUTH_HEADER)
        rest = resp.json()["restaurants"][0]
        for key in ("id", "name", "whatsapp_number", "created_at", "orders_30d",
                    "conversations_30d", "last_order_at", "last_conversation_at",
                    "has_billing", "has_menu", "staff_count", "onboarding_score"):
            assert key in rest, f"Missing key: {key}"

    def test_restaurants_empty_list(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._pool_with_fetch([])):
            resp = client.get("/api/analytics/restaurants", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["restaurants"] == []


# ── 4. test_trends_returns_daily_data ────────────────────────────────────────

class TestTrends:
    def test_trends_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        resp = client.get("/api/analytics/trends")
        assert resp.status_code == 401

    def _pool_with_two_fetches(self, order_rows, conv_rows):
        """Pool whose conn.fetch returns order_rows first, conv_rows second."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[order_rows, conv_rows])
        acquire_cm = AsyncMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.acquire = MagicMock(return_value=acquire_cm)
        return AsyncMock(return_value=pool)

    def _make_daily_rows(self, dates_counts):
        rows = []
        for d, c in dates_counts:
            row = MagicMock()
            row.__getitem__ = lambda s, k, _d=d, _c=c: _d if k == "date" else _c
            rows.append(row)
        return rows

    def test_trends_returns_200_with_keys(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        o_rows = self._make_daily_rows([("2026-03-14", 89), ("2026-03-15", 102)])
        c_rows = self._make_daily_rows([("2026-03-14", 234)])
        with patch(_PATCH_TARGET, self._pool_with_two_fetches(o_rows, c_rows)):
            resp = client.get("/api/analytics/trends", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert "daily_orders" in body
        assert "daily_conversations" in body

    def test_trends_date_format(self, client, monkeypatch):
        """Dates must be YYYY-MM-DD strings."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        o_rows = self._make_daily_rows([("2026-03-14", 89)])
        c_rows = self._make_daily_rows([("2026-03-14", 200)])
        with patch(_PATCH_TARGET, self._pool_with_two_fetches(o_rows, c_rows)):
            resp = client.get("/api/analytics/trends", headers=AUTH_HEADER)
        orders = resp.json()["daily_orders"]
        assert len(orders) == 1
        entry = orders[0]
        assert "date" in entry and "count" in entry
        assert entry["date"] == "2026-03-14"
        assert entry["count"] == 89

    def test_trends_count_values(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        o_rows = self._make_daily_rows([("2026-03-14", 89), ("2026-03-15", 102)])
        c_rows = self._make_daily_rows([("2026-03-14", 234), ("2026-03-15", 210)])
        with patch(_PATCH_TARGET, self._pool_with_two_fetches(o_rows, c_rows)):
            resp = client.get("/api/analytics/trends", headers=AUTH_HEADER)
        body = resp.json()
        assert body["daily_orders"][0]["count"] == 89
        assert body["daily_orders"][1]["count"] == 102
        assert body["daily_conversations"][0]["count"] == 234

    def test_trends_empty_tables(self, client, monkeypatch):
        """Empty tables → 200 with empty lists (not 500)."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._pool_with_two_fetches([], [])):
            resp = client.get("/api/analytics/trends", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert body["daily_orders"] == []
        assert body["daily_conversations"] == []

    def test_trends_query_failure_returns_empty_lists(self, client, monkeypatch):
        """If DB query raises, the list falls back to [] and still returns 200."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=Exception("table not found"))
        acquire_cm = AsyncMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.acquire = MagicMock(return_value=acquire_cm)
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            resp = client.get("/api/analytics/trends", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert body["daily_orders"] == []


# ── Page route ────────────────────────────────────────────────────────────────

class TestAnalyticsPage:
    def test_page_returns_html(self, client):
        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
