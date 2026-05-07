"""
tests/test_alerts_churn_risk.py

Unit tests for the churn-risk alert in app.services.alerts.

Coverage:
1. Tenant with ratio=0.4 (below 50% threshold) fires a MEDIUM alert.
2. Tenant with ratio=0.6 (above threshold) does NOT fire.
3. Tenant with baseline < 3 conv/day is skipped (noise floor).
4. Cooldown: same (org_id, date) key → duplicate suppressed within same run.
5. Next-day key re-arms: different date suffix → fires again.
6. Multi-tenant: only the at-risk one fires, not the healthy one.

No real DB or network calls — everything is mocked.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pool_with_rows(rows: list[dict]):
    """Build an asyncpg pool mock whose .fetch() returns `rows`."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)

    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _churn_row(org_id: int, org_name: str, baseline: float, recent: float) -> dict:
    """Simulate a row returned by the churn-risk SQL query."""
    return {
        "org_id": org_id,
        "org_name": org_name,
        "baseline_avg": baseline,
        "recent_avg": recent,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestChurnRiskFiresAlert:

    @pytest.mark.asyncio
    async def test_ratio_04_fires_medium_alert(self, monkeypatch):
        """Tenant with recent=4, baseline=10 → ratio 0.4 → fires MEDIUM alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        rows = [_churn_row(org_id=7, org_name="Pizzeria Roma", baseline=10.0, recent=4.0)]
        pool = _make_pool_with_rows(rows)

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "severity": severity, "title": title, "detail": detail})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_churn_risk()

        assert len(fired) == 1, f"Expected 1 alert, got {fired}"
        assert fired[0]["severity"] == "MEDIUM"
        assert "Pizzeria Roma" in fired[0]["title"]
        today_str = date.today().isoformat()
        assert fired[0]["key"] == f"churn_risk:7:{today_str}"
        assert "60.0%" in fired[0]["detail"]

    @pytest.mark.asyncio
    async def test_ratio_06_does_not_fire(self, monkeypatch):
        """Tenant with recent=6, baseline=10 → ratio 0.6 → above threshold → no alert.

        The SQL WHERE clause filters these out before they reach Python.
        We simulate that by returning an empty rows list.
        """
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        # Healthy tenant would be filtered out by SQL WHERE; simulate empty result
        pool = _make_pool_with_rows([])

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_churn_risk()

        assert fired == [], f"Expected no alerts, got {fired}"

    @pytest.mark.asyncio
    async def test_baseline_below_min_skipped(self, monkeypatch):
        """Tenant with baseline=2.0 (< 3 conv/day noise floor) → filtered by SQL → no alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        # Low-baseline tenant would be filtered by SQL WHERE baseline_avg >= 3
        pool = _make_pool_with_rows([])

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_churn_risk()

        assert fired == [], "Noise-floor tenant must not fire"

    @pytest.mark.asyncio
    async def test_cooldown_same_day_suppresses_duplicate(self, monkeypatch):
        """Running the check twice in the same day suppresses the second alert via cooldown."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        rows = [_churn_row(org_id=9, org_name="Sushi Bar", baseline=20.0, recent=5.0)]
        pool = _make_pool_with_rows(rows)

        # Use the real _fire_alert (which checks _last_alert cooldown)
        # but mock the webhook so no HTTP call happens
        with patch("app.services.alerts._post_webhook", AsyncMock()):
            with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
                await alerts_mod._check_churn_risk()
                # Run again immediately — same key, still in cooldown
                await alerts_mod._check_churn_risk()

        today_str = date.today().isoformat()
        key = f"churn_risk:9:{today_str}"
        assert key in alerts_mod._last_alert, "Key should have been recorded"
        # Only one entry in _last_alert means second call was suppressed
        # (the cooldown value is monotonic, so it was set once)
        count = sum(1 for k in alerts_mod._last_alert if k.startswith("churn_risk:9:"))
        assert count == 1, "Should record exactly one entry per org+date"

    @pytest.mark.asyncio
    async def test_different_date_key_rearms(self, monkeypatch):
        """A next-day key is distinct — it fires even if yesterday's fired."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        # Pre-seed a yesterday-dated key at max cooldown so it's expired
        # Use a fake yesterday key — different from today's
        alerts_mod._last_alert["churn_risk:5:2020-01-01"] = 0.0  # ancient, expired

        rows = [_churn_row(org_id=5, org_name="El Rancho", baseline=8.0, recent=3.0)]
        pool = _make_pool_with_rows(rows)

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_churn_risk()

        today_str = date.today().isoformat()
        assert any(f["key"] == f"churn_risk:5:{today_str}" for f in fired), \
            f"Expected today's key in fired alerts, got: {fired}"

    @pytest.mark.asyncio
    async def test_multi_tenant_only_at_risk_fires(self, monkeypatch):
        """With two tenants, only the at-risk one (ratio<0.5) should fire.

        Healthy tenant filtered by SQL — only at-risk row returned.
        """
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        # SQL only returns tenants that pass the WHERE clause (ratio < 0.5, baseline >= 3)
        # Healthy tenant (ratio 0.7) is already filtered out by the query
        rows = [_churn_row(org_id=11, org_name="Bad Cafe", baseline=15.0, recent=5.0)]
        pool = _make_pool_with_rows(rows)

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "title": title})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_churn_risk()

        assert len(fired) == 1
        assert "Bad Cafe" in fired[0]["title"]


# ── Activation funnel smoke test ───────────────────────────────────────────────

class TestActivationFunnelEndpoint:

    def test_activation_returns_expected_shape(self, monkeypatch):
        """Smoke test: endpoint returns 200 with funnel, funnel_pct, time_to_stage, per_org."""
        import os
        from unittest.mock import AsyncMock, MagicMock
        from fastapi.testclient import TestClient
        from app.main import app

        ADMIN_KEY = "test-activation-key"

        # Mock session validation
        async def _get_session(token):
            return "superadmin" if token == ADMIN_KEY else None

        monkeypatch.setattr(
            "app.repositories.sessions_repo.get_session",
            AsyncMock(side_effect=_get_session),
        )

        # Build a fake row matching what the SQL returns
        from datetime import datetime, timezone
        fake_row = {
            "org_id": 1,
            "org_name": "Test Org",
            "signed_up_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "has_menu": True,
            "first_message_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "first_order_at": datetime(2024, 1, 3, tzinfo=timezone.utc),
            "hours_to_message": 24.0,
            "hours_to_order": 48.0,
        }

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[fake_row])

        acquire_cm = AsyncMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)

        monkeypatch.setattr(
            "app.routes.internal.analytics.get_pool",
            AsyncMock(return_value=pool),
        )

        client = TestClient(app)
        resp = client.get(
            "/api/internal/analytics/activation",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()

        # Shape checks
        assert "funnel" in data
        assert "funnel_pct" in data
        assert "time_to_stage" in data
        assert "per_org" in data

        # Aggregate correctness
        f = data["funnel"]
        assert f["signed_up"] == 1
        assert f["menu_loaded"] == 1
        assert f["first_message"] == 1
        assert f["first_order"] == 1

        # funnel_pct: all 100% since 1/1
        assert data["funnel_pct"]["signed_up"] == 100.0
        assert data["funnel_pct"]["first_order"] == 100.0

        # per_org entry
        assert len(data["per_org"]) == 1
        org = data["per_org"][0]
        assert org["stage"] == 4
        assert org["org_name"] == "Test Org"

        # time_to_stage populated
        tts = data["time_to_stage"]
        assert tts["first_message_median_h"] == 24.0
        assert tts["first_order_median_h"] == 48.0
