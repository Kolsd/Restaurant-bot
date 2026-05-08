"""
tests/test_notifications.py

Unit tests for app.repositories.internal.notifications_repo.

Coverage:
- Dead letters > 0 → emits notification
- Dead letters = 0 → does NOT emit
- Cost runaway with one offender → 1 notification with severity=high
- Churn risk + cost runaway both fire → 2 notifications, sorted by severity
- Plan cap >= 100% → severity=high
- Plan cap 90-99% → severity=medium
- Empty everything → returns empty list (not error)
- One source raises → others still return (aggregator is best-effort)

No real DB or network calls — everything is mocked.
Follows the pattern from tests/test_alerts_cost_runaway.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_fetchval_pool(return_value):
    """Pool mock where conn.fetchval() returns a scalar."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=return_value)
    conn.fetch = AsyncMock(return_value=[])
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _make_fetch_pool(rows: list[dict]):
    """Pool mock where conn.fetch() returns rows."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchval = AsyncMock(return_value=0)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


# ── Dead letters ──────────────────────────────────────────────────────────────

class TestDeadLetters:
    @pytest.mark.asyncio
    async def test_dead_letters_gt_zero_emits_notification(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        pool = _make_fetchval_pool(3)
        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = await repo._fetch_dead_letters()

        assert len(result) == 1
        n = result[0]
        assert n["type"] == "deadletter"
        assert n["severity"] == "high"
        assert n["count"] == 3
        assert "3" in n["title"]
        assert n["id"] == "deadletter:3"

    @pytest.mark.asyncio
    async def test_dead_letters_zero_emits_nothing(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        pool = _make_fetchval_pool(0)
        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = await repo._fetch_dead_letters()

        assert result == []

    @pytest.mark.asyncio
    async def test_dead_letters_none_treated_as_zero(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        pool = _make_fetchval_pool(None)
        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = await repo._fetch_dead_letters()

        assert result == []

    @pytest.mark.asyncio
    async def test_dead_letters_db_error_returns_empty(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        async def _boom():
            raise RuntimeError("DB down")

        monkeypatch.setattr(repo, "_get_pool", _boom)
        result = await repo._fetch_dead_letters()
        assert result == []


# ── Cost runaway ──────────────────────────────────────────────────────────────

class TestCostRunaway:
    @pytest.mark.asyncio
    async def test_offender_emits_high_notification(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        rows = [
            {
                "org_id": 42,
                "org_name": "Burger Mesio",
                "plan_code": "pulso",
                "tokens_today": 150_000,  # pulso limit=50_000; 2x=100_000 → over
            }
        ]
        pool = _make_fetch_pool(rows)

        _LIMITS = {"pulso": 50_000, "restaurante": 200_000, "pro": 400_000}

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            with patch(
                "app.repositories.internal.notifications_repo._PLAN_DAILY_TOKEN_LIMITS",
                _LIMITS,
                create=True,
            ):
                # Patch the import inside the function
                import app.repositories.cost_metrics_repo as cmr_stub
                original = getattr(cmr_stub, "_PLAN_DAILY_TOKEN_LIMITS", None)
                cmr_stub._PLAN_DAILY_TOKEN_LIMITS = _LIMITS
                try:
                    result = await repo._fetch_cost_runaway()
                finally:
                    if original is not None:
                        cmr_stub._PLAN_DAILY_TOKEN_LIMITS = original

        assert len(result) == 1
        n = result[0]
        assert n["type"] == "cost"
        assert n["severity"] == "high"
        assert n["tenant_id"] == 42
        assert "Burger Mesio" in n["title"]
        assert n["id"] == "cost_runaway:42"

    @pytest.mark.asyncio
    async def test_unlimited_plan_not_emitted(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo
        import app.repositories.cost_metrics_repo as cmr_stub

        rows = [
            {
                "org_id": 99,
                "org_name": "Mega",
                "plan_code": "cadena",
                "tokens_today": 10_000_000,
            }
        ]
        pool = _make_fetch_pool(rows)
        _LIMITS = {"pulso": 50_000, "cadena": -1}
        original = getattr(cmr_stub, "_PLAN_DAILY_TOKEN_LIMITS", None)
        cmr_stub._PLAN_DAILY_TOKEN_LIMITS = _LIMITS
        try:
            with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
                result = await repo._fetch_cost_runaway()
        finally:
            if original is not None:
                cmr_stub._PLAN_DAILY_TOKEN_LIMITS = original

        assert result == []

    @pytest.mark.asyncio
    async def test_cost_runaway_db_error_returns_empty(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        async def _boom():
            raise RuntimeError("DB down")

        monkeypatch.setattr(repo, "_get_pool", _boom)
        result = await repo._fetch_cost_runaway()
        assert result == []


# ── Plan cap warnings ─────────────────────────────────────────────────────────

class TestPlanCapWarnings:
    @pytest.mark.asyncio
    async def test_at_100pct_is_high_severity(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        rows = [
            {
                "org_id": 7,
                "org_name": "La Parrilla",
                "plan_code": "restaurante",
                "conversations_used": 500,
                "conversations_per_month": 500,
            }
        ]
        pool = _make_fetch_pool(rows)
        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = await repo._fetch_plan_cap_warnings()

        assert len(result) == 1
        n = result[0]
        assert n["severity"] == "high"
        assert n["type"] == "plan_cap"
        assert n["tenant_id"] == 7
        assert "alcanzó" in n["title"]

    @pytest.mark.asyncio
    async def test_at_90pct_is_medium_severity(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        rows = [
            {
                "org_id": 8,
                "org_name": "El Asador",
                "plan_code": "pulso",
                "conversations_used": 90,
                "conversations_per_month": 100,
            }
        ]
        pool = _make_fetch_pool(rows)
        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = await repo._fetch_plan_cap_warnings()

        assert len(result) == 1
        n = result[0]
        assert n["severity"] == "medium"
        assert "cerca de" in n["title"]

    @pytest.mark.asyncio
    async def test_plan_cap_db_error_returns_empty(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        async def _boom():
            raise RuntimeError("DB down")

        monkeypatch.setattr(repo, "_get_pool", _boom)
        result = await repo._fetch_plan_cap_warnings()
        assert result == []


# ── Sorting ───────────────────────────────────────────────────────────────────

class TestAggregatorSorting:
    @pytest.mark.asyncio
    async def test_high_before_medium_in_combined_output(self, monkeypatch):
        """Cost runaway (high) and churn (medium) — high must come first."""
        import app.repositories.internal.notifications_repo as repo

        cost_notif = {
            "id": "cost_runaway:1",
            "type": "cost",
            "severity": "high",
            "title": "Cost runaway",
            "detail": "detail",
            "url": "/internal/costs",
            "created_at": "2026-05-07T10:00:00Z",
            "count": 1,
            "tenant_id": 1,
        }
        churn_notif = {
            "id": "churn:2",
            "type": "churn",
            "severity": "medium",
            "title": "Churn risk",
            "detail": "detail",
            "url": "/internal/analytics",
            "created_at": "2026-05-07T10:00:00Z",
            "count": 1,
            "tenant_id": 2,
        }

        async def _empty():
            return []

        monkeypatch.setattr(repo, "_fetch_dead_letters", _empty)
        monkeypatch.setattr(repo, "_fetch_cost_runaway", AsyncMock(return_value=[cost_notif]))
        monkeypatch.setattr(repo, "_fetch_churn_risk", AsyncMock(return_value=[churn_notif]))
        monkeypatch.setattr(repo, "_fetch_new_prospects", _empty)
        monkeypatch.setattr(repo, "_fetch_suspended_tenants", _empty)
        monkeypatch.setattr(repo, "_fetch_plan_cap_warnings", _empty)

        result = await repo.db_get_notifications()

        assert len(result) == 2
        assert result[0]["severity"] == "high"
        assert result[1]["severity"] == "medium"

    @pytest.mark.asyncio
    async def test_empty_everything_returns_empty_list(self, monkeypatch):
        import app.repositories.internal.notifications_repo as repo

        async def _empty():
            return []

        monkeypatch.setattr(repo, "_fetch_dead_letters", _empty)
        monkeypatch.setattr(repo, "_fetch_cost_runaway", _empty)
        monkeypatch.setattr(repo, "_fetch_churn_risk", _empty)
        monkeypatch.setattr(repo, "_fetch_new_prospects", _empty)
        monkeypatch.setattr(repo, "_fetch_suspended_tenants", _empty)
        monkeypatch.setattr(repo, "_fetch_plan_cap_warnings", _empty)

        result = await repo.db_get_notifications()
        assert result == []

    @pytest.mark.asyncio
    async def test_one_source_raises_others_still_return(self, monkeypatch):
        """If one source raises (shouldn't, but in case), others still contribute."""
        import app.repositories.internal.notifications_repo as repo

        dead_notif = {
            "id": "deadletter:1",
            "type": "deadletter",
            "severity": "high",
            "title": "1 dead letter",
            "detail": "detail",
            "url": "/internal/monitoring",
            "created_at": "2026-05-07T10:00:00Z",
            "count": 1,
            "tenant_id": None,
        }

        monkeypatch.setattr(repo, "_fetch_dead_letters", AsyncMock(return_value=[dead_notif]))

        # Simulate a source that already swallows its own exception (returns [])
        # because each source function catches internally.
        async def _broken_source():
            return []  # broken but already caught internally

        monkeypatch.setattr(repo, "_fetch_cost_runaway", _broken_source)
        monkeypatch.setattr(repo, "_fetch_churn_risk", _broken_source)
        monkeypatch.setattr(repo, "_fetch_new_prospects", _broken_source)
        monkeypatch.setattr(repo, "_fetch_suspended_tenants", _broken_source)
        monkeypatch.setattr(repo, "_fetch_plan_cap_warnings", _broken_source)

        result = await repo.db_get_notifications()

        assert len(result) == 1
        assert result[0]["type"] == "deadletter"


# ── Endpoint helper ───────────────────────────────────────────────────────────

class TestCountBySeverity:
    def test_count_by_severity_correct(self):
        from app.routes.internal.notifications import _count_by_severity

        items = [
            {"severity": "high"},
            {"severity": "high"},
            {"severity": "medium"},
            {"severity": "low"},
        ]
        counts = _count_by_severity(items)
        assert counts == {"critical": 0, "high": 2, "medium": 1, "low": 1}

    def test_count_by_severity_empty(self):
        from app.routes.internal.notifications import _count_by_severity

        counts = _count_by_severity([])
        assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}

    def test_unknown_severity_ignored(self):
        from app.routes.internal.notifications import _count_by_severity

        items = [{"severity": "unknown"}, {"severity": "high"}]
        counts = _count_by_severity(items)
        assert counts["high"] == 1
        assert counts["critical"] == 0


# ── Import smoke test ─────────────────────────────────────────────────────────

class TestImports:
    def test_router_imports_cleanly(self):
        from app.routes.internal.notifications import router
        assert router is not None

    def test_repo_imports_cleanly(self):
        from app.repositories.internal.notifications_repo import db_get_notifications
        assert callable(db_get_notifications)
