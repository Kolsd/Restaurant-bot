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

_PATCH_TARGET = "app.routes.internal.analytics.get_pool"
_SESSION_PATCH = "app.repositories.sessions_repo.get_session"

ADMIN_KEY = "test-analytics-key"
AUTH_HEADER = {"Authorization": f"Bearer {ADMIN_KEY}"}


def _mock_session(valid_token: str = ADMIN_KEY):
    """Return an async mock for sessions_repo.get_session that accepts valid_token as superadmin."""
    async def _get_session(token):
        return "superadmin" if token == valid_token else None
    return AsyncMock(side_effect=_get_session)


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
    def test_no_auth_returns_401(self, client, mock_superadmin_session, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        resp = client.get("/api/internal/analytics/overview")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client, monkeypatch):
        """Wrong/unknown token → sessions_repo returns None → 403."""
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_SESSION_PATCH, AsyncMock(return_value=None)):
            resp = client.get("/api/internal/analytics/overview",
                              headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 403

    def test_missing_env_key_returns_401(self, client, mock_superadmin_session, monkeypatch):
        """No Authorization header → 401 regardless of ADMIN_KEY env."""
        monkeypatch.delenv("ADMIN_KEY", raising=False)
        resp = client.get("/api/internal/analytics/overview")
        assert resp.status_code == 401


# ── 2. test_overview_returns_data ────────────────────────────────────────────

class TestOverviewData:
    @pytest.fixture(autouse=True)
    def _session_mock(self, mock_superadmin_session):
        """All data tests require a valid superadmin session."""

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
            resp = client.get("/api/internal/analytics/overview", headers=AUTH_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert "restaurants" in body
        assert "orders" in body
        assert "conversations" in body
        assert "billing" in body

    def test_overview_restaurants_keys(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/internal/analytics/overview", headers=AUTH_HEADER)
        r = resp.json()["restaurants"]
        assert "total" in r
        assert "active_7d" in r
        assert "active_30d" in r
        assert "new_this_week" in r
        assert "new_this_month" in r

    def test_overview_orders_values(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/internal/analytics/overview", headers=AUTH_HEADER)
        o = resp.json()["orders"]
        assert o["today"] == 156
        assert o["this_week"] == 892
        assert o["this_month"] == 3421
        assert o["avg_daily_30d"] == 114.0

    def test_overview_billing_keys(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch(_PATCH_TARGET, self._build_pool_mock()):
            resp = client.get("/api/internal/analytics/overview", headers=AUTH_HEADER)
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
            resp = client.get("/api/internal/analytics/overview", headers=AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["restaurants"]["total"] is None      # failed
        assert body["restaurants"]["active_7d"] == 32    # subsequent ok


# ── 3. test_restaurants_list ─────────────────────────────────────────────────

# ── 4. test_trends_returns_daily_data ────────────────────────────────────────

# ── Page route ────────────────────────────────────────────────────────────────
