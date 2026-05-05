"""
tests/test_churn_tier.py — Polish batch GAP A.

Covers churn_tier() classifier and integration into get_at_risk_customers
result rows. Pure unit tests, no DB.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from tests.conftest import make_pool, make_row
from app.services.tenant_context import tenant_scope as _tenant_scope


def _run_scoped(restaurant_id: int, coro):
    async def _wrapper():
        with _tenant_scope(restaurant_id):
            return await coro
    return asyncio.get_event_loop().run_until_complete(_wrapper())


# ── Pure helper ──────────────────────────────────────────────────────────────


class TestChurnTierBuckets:
    """Boundary checks for the 5 tiers."""

    def test_active_bucket_zero_days(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(0, 5) == "active"

    def test_active_bucket_upper_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(14, 5) == "active"

    def test_cooling_lower_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(15, 5) == "cooling"

    def test_cooling_upper_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(30, 5) == "cooling"

    def test_at_risk_lower_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(31, 5) == "at_risk"

    def test_at_risk_upper_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(60, 5) == "at_risk"

    def test_churned_lower_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(61, 5) == "churned"

    def test_churned_upper_boundary(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(120, 5) == "churned"

    def test_lost_just_over_120(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(121, 5) == "lost"

    def test_lost_far_future(self):
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(365, 5) == "lost"

    def test_negative_days_coerced_to_active(self):
        """Defensive: clock skew can produce negative values."""
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(-3, 5) == "active"

    def test_none_days_safe(self):
        """Falsey / None days should not raise."""
        from app.repositories.marketing_repo import churn_tier
        assert churn_tier(None, 5) == "active"  # type: ignore[arg-type]

    def test_total_orders_optional(self):
        """total_orders has a default and does not affect classification today."""
        from app.repositories.marketing_repo import churn_tier
        # Same days_since must yield same tier regardless of total_orders.
        assert churn_tier(45) == churn_tier(45, 0) == churn_tier(45, 50) == "at_risk"


# ── Integration: tier appears in get_at_risk_customers rows ──────────────────


class TestAtRiskCustomersIncludesTier:

    @staticmethod
    def _make_conn(rows):
        from unittest.mock import MagicMock
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=rows)
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="UPDATE 1")
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn)
        return conn

    @staticmethod
    def _row(phone: str, days_since: int, total_orders: int, total_spent: Decimal):
        from datetime import datetime, timedelta, timezone
        return make_row({
            "customer_phone": phone,
            "customer_name":  f"Cliente {phone[-3:]}",
            "last_order_at":  datetime.now(tz=timezone.utc) - timedelta(days=days_since),
            "days_since":     days_since,
            "total_orders":   total_orders,
            "total_spent":    total_spent,
        })

    def test_tier_field_present_at_risk(self, monkeypatch):
        from app.repositories import marketing_repo

        rows = [self._row("+5730033", 45, 7, Decimal("180000"))]
        conn = self._make_conn(rows)
        pool = make_pool(conn)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = _run_scoped(1, marketing_repo.get_at_risk_customers(1))

        assert len(result) == 1
        assert result[0]["tier"] == "at_risk"
        assert result[0]["days_since"] == 45

    def test_tier_field_present_lost(self, monkeypatch):
        from app.repositories import marketing_repo

        rows = [self._row("+5730099", 200, 12, Decimal("500000"))]
        conn = self._make_conn(rows)
        pool = make_pool(conn)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = _run_scoped(1, marketing_repo.get_at_risk_customers(1))

        assert result[0]["tier"] == "lost"

    def test_tier_field_present_churned(self, monkeypatch):
        from app.repositories import marketing_repo

        rows = [self._row("+5730088", 90, 9, Decimal("400000"))]
        conn = self._make_conn(rows)
        pool = make_pool(conn)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            result = _run_scoped(1, marketing_repo.get_at_risk_customers(1))

        assert result[0]["tier"] == "churned"
