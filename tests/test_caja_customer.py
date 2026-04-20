"""
Unit tests for GET /api/caja/customer/{phone}.

All external I/O is mocked — no live DB or network required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────────────

RESTAURANT = {
    "id": 1,
    "org_id": 1,
    "location_id": 1,
    "name": "Test Restaurant",
    "whatsapp_number": "+573001234567",
    "features": {},
}

PROFILE_KNOWN = {
    "id": 42,
    "org_id": 1,
    "phone": "+57300111222",
    "display_name": "Ana María Restrepo",
    "preferences": {},
    "last_order_summary": "Ajiaco · Limonada",
    "total_orders": 24,
    "total_spent": 1240000,
    "first_seen": datetime(2024, 8, 1, 10, 0, 0, tzinfo=timezone.utc),
    "last_seen": datetime(2026, 4, 15, 12, 34, 56, tzinfo=timezone.utc),
}

LOYALTY_BALANCE = {"puntos_actuales": 1240, "equivalencia_cop": 12400}

RECENT_ORDERS_MOCK = [
    {
        "id": "#3401AB",
        "total": 62300.0,
        "created_at": "2026-04-15T12:34:56+00:00",
        "items_summary": "Ajiaco · Limonada",
        "source": "delivery",
    }
]


def _make_restaurant_dep(restaurant: dict):
    """Override get_current_restaurant_scoped to yield the given dict inside tenant_scope."""
    from contextlib import contextmanager
    from app.services.tenant_context import tenant_scope

    async def _override(request=None):
        with tenant_scope(restaurant["id"]):
            yield restaurant

    return _override


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCajaCustomerKnown:
    def test_known_customer_returns_full_payload(self, monkeypatch):
        """Known customer with profile, loyalty balance, and recent orders."""
        from app.routes.deps import get_current_restaurant_scoped
        app.dependency_overrides[get_current_restaurant_scoped] = _make_restaurant_dep(RESTAURANT)

        with (
            patch(
                "app.repositories.customer_profiles_repo.get_profile",
                AsyncMock(return_value=PROFILE_KNOWN),
            ),
            patch(
                "app.repositories.loyalty_repo.db_get_loyalty_balance",
                AsyncMock(return_value=LOYALTY_BALANCE),
            ),
            patch(
                "app.routes.tables._get_recent_orders_for_phone",
                AsyncMock(return_value=RECENT_ORDERS_MOCK),
            ),
        ):
            client = TestClient(app)
            resp = client.get(
                "/api/caja/customer/%2B57300111222",
                headers={"Authorization": "Bearer test_token"},
            )

        app.dependency_overrides.pop(get_current_restaurant_scoped, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_known"] is True
        assert body["name"] == "Ana María Restrepo"
        assert body["stats"]["total_orders"] == 24
        assert body["stats"]["total_spent"] == 1240000.0
        assert body["stats"]["last_seen"] is not None
        assert body["stats"]["first_seen"] is not None
        assert body["loyalty"]["points"] == 1240
        assert body["loyalty"]["tier"] is None
        assert len(body["recent_orders"]) == 1


class TestCajaCustomerUnknown:
    def test_unknown_customer_returns_is_known_false(self, monkeypatch):
        """Phone with no customer_profile returns is_known=False."""
        from app.routes.deps import get_current_restaurant_scoped
        app.dependency_overrides[get_current_restaurant_scoped] = _make_restaurant_dep(RESTAURANT)

        with (
            patch(
                "app.repositories.customer_profiles_repo.get_profile",
                AsyncMock(return_value=None),
            ),
        ):
            client = TestClient(app)
            resp = client.get(
                "/api/caja/customer/%2B57999000000",
                headers={"Authorization": "Bearer test_token"},
            )

        app.dependency_overrides.pop(get_current_restaurant_scoped, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_known"] is False
        assert body["name"] is None
        assert body["stats"] == {}
        assert body["loyalty"] is None
        assert body["recent_orders"] == []


class TestCajaCustomerLoyaltyDisabled:
    def test_loyalty_disabled_returns_loyalty_null(self, monkeypatch):
        """When loyalty lookup returns None (no record), loyalty=null."""
        from app.routes.deps import get_current_restaurant_scoped
        app.dependency_overrides[get_current_restaurant_scoped] = _make_restaurant_dep(RESTAURANT)

        with (
            patch(
                "app.repositories.customer_profiles_repo.get_profile",
                AsyncMock(return_value=PROFILE_KNOWN),
            ),
            patch(
                "app.repositories.loyalty_repo.db_get_loyalty_balance",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.routes.tables._get_recent_orders_for_phone",
                AsyncMock(return_value=[]),
            ),
        ):
            client = TestClient(app)
            resp = client.get(
                "/api/caja/customer/%2B57300111222",
                headers={"Authorization": "Bearer test_token"},
            )

        app.dependency_overrides.pop(get_current_restaurant_scoped, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_known"] is True
        assert body["loyalty"] is None


class TestCajaCustomerLoyaltyError:
    def test_loyalty_exception_returns_loyalty_null(self, monkeypatch):
        """When loyalty lookup raises an exception, loyalty=null (best-effort)."""
        from app.routes.deps import get_current_restaurant_scoped
        app.dependency_overrides[get_current_restaurant_scoped] = _make_restaurant_dep(RESTAURANT)

        with (
            patch(
                "app.repositories.customer_profiles_repo.get_profile",
                AsyncMock(return_value=PROFILE_KNOWN),
            ),
            patch(
                "app.repositories.loyalty_repo.db_get_loyalty_balance",
                AsyncMock(side_effect=Exception("DB down")),
            ),
            patch(
                "app.routes.tables._get_recent_orders_for_phone",
                AsyncMock(return_value=[]),
            ),
        ):
            client = TestClient(app)
            resp = client.get(
                "/api/caja/customer/%2B57300111222",
                headers={"Authorization": "Bearer test_token"},
            )

        app.dependency_overrides.pop(get_current_restaurant_scoped, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_known"] is True
        assert body["loyalty"] is None


class TestCajaCustomerPhoneNormalization:
    def test_phone_url_decoded(self, monkeypatch):
        """URL-encoded phone (%2B57...) is decoded before lookup."""
        from app.routes.deps import get_current_restaurant_scoped
        app.dependency_overrides[get_current_restaurant_scoped] = _make_restaurant_dep(RESTAURANT)

        captured_phone = []

        async def _mock_profile(org_id, phone):
            captured_phone.append(phone)
            return None

        with patch("app.repositories.customer_profiles_repo.get_profile", _mock_profile):
            client = TestClient(app)
            resp = client.get(
                "/api/caja/customer/%2B57300111222",
                headers={"Authorization": "Bearer test_token"},
            )

        app.dependency_overrides.pop(get_current_restaurant_scoped, None)

        assert resp.status_code == 200
        assert captured_phone == ["+57300111222"]
