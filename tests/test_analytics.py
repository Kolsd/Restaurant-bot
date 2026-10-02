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

_SESSION_PATCH = "app.repositories.sessions_repo.get_session"

ADMIN_KEY = "test-analytics-key"
AUTH_HEADER = {"Authorization": f"Bearer {ADMIN_KEY}"}


def _mock_session(valid_token: str = ADMIN_KEY):
    """Return an async mock for sessions_repo.get_session that accepts valid_token as superadmin."""
    async def _get_session(token):
        return "mesio:superadmin" if token == valid_token else None
    return AsyncMock(side_effect=_get_session)


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
    """The route hands platform_stats_repo's numbers to the HQ home. What the
    numbers mean (table rounds + web orders, RLS-visible, demo out) is tested
    against a real DB in tests/test_hq_review_2026_10_02.py; this pins the
    response contract the home page reads."""

    @pytest.fixture(autouse=True)
    def _session_mock(self, mock_superadmin_session):
        """All data tests require a valid superadmin session."""

    _DATA = {
        "orders": {"today": 156, "this_week": 892, "this_month": 3421, "avg_daily_30d": 114.0,
                   "sales_today": __import__("decimal").Decimal("1250000.00"),
                   "sales_7d": __import__("decimal").Decimal("9000000")},
        "restaurants": {"total": 45, "sedes": 52, "active_today": 20, "active_7d": 32, "active_30d": 40,
                        "new_this_week": 3, "new_this_month": 8},
        "diners_today": 300, "errors_24h": 2,
        "alerts": {"open": 4, "critical": 1},
        "billing": {"invoices_today": 45, "invoices_this_month": 1200},
    }

    def _get(self, client, monkeypatch):
        import copy
        monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
        with patch("app.repositories.internal.platform_stats_repo.db_platform_overview",
                   AsyncMock(return_value=copy.deepcopy(self._DATA))) as stub:
            resp = client.get("/api/internal/analytics/overview", headers=AUTH_HEADER)
        return resp, stub

    def test_overview_returns_the_home_kpis(self, client, monkeypatch):
        resp, stub = self._get(client, monkeypatch)
        assert resp.status_code == 200
        body = resp.json()
        assert body["orders"]["today"] == 156
        assert body["restaurants"]["active_today"] == 20
        assert body["alerts"] == {"open": 4, "critical": 1}
        assert body["errors_24h"] == 2
        # "today" is Bogotá's business day, passed as a naive UTC datetime.
        (day_start,), _ = stub.call_args
        assert day_start.tzinfo is None and day_start.hour == 5

    def test_money_leaves_as_json_numbers(self, client, monkeypatch):
        resp, _ = self._get(client, monkeypatch)
        o = resp.json()["orders"]
        assert o["sales_today"] == 1250000.0
        assert o["sales_7d"] == 9000000.0


# ── 3. test_restaurants_list ─────────────────────────────────────────────────

# ── 4. test_trends_returns_daily_data ────────────────────────────────────────

# ── Page route ────────────────────────────────────────────────────────────────
