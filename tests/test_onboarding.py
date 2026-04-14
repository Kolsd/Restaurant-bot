"""
tests/test_onboarding.py

Tests for GET /api/onboarding/status.

All tests are unit-level (no real DB).  DB calls in the endpoint are
patched via monkeypatch so the suite runs without a live database.

Fixtures used:
  client      — TestClient(app) from conftest.py
  monkeypatch — pytest built-in
  patch_auth  — helper from conftest.py that wires up verify_token + db_get_user
                + db_get_restaurant_by_id

After the repo-extraction refactor, the endpoint delegates to:
  - app.routes.settings_routes.db_has_staff          (staff_repo)
  - app.routes.settings_routes.db_has_orders_by_bot_number  (restaurant_repo)
These are patched directly instead of patching get_pool.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import database as db

# Targets for the two repo-function patches now used by the endpoint
_PATCH_HAS_STAFF  = "app.routes.settings_routes.db_has_staff"
_PATCH_HAS_ORDERS = "app.routes.settings_routes.db_has_orders_by_bot_number"

URL = "/api/onboarding/status"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_repo_patches(has_staff: bool = False, has_order: bool = False):
    """Return (has_staff_mock, has_orders_mock) for use with patch()."""
    return AsyncMock(return_value=has_staff), AsyncMock(return_value=has_order)


def _patch_restaurant(monkeypatch, *, restaurant_id=1,
                      whatsapp_number="+573001234567",
                      features=None, menu=None,
                      username="owner_test", role="owner"):
    """
    Patch auth + restaurant lookup.  Unlike the generic conftest patch_auth,
    this also lets us control the `menu` field on the restaurant dict.
    """
    if features is None:
        features = {}

    restaurant = {
        "id":              restaurant_id,
        "name":            "Test Restaurant",
        "whatsapp_number": whatsapp_number,
        "features":        features,
        "menu":            menu,
    }
    user = {
        "username":        username,
        "restaurant_name": "Test Restaurant",
        "branch_id":       restaurant_id,
        "role":            role,
        "password_hash":   "$2b$12$placeholder",
    }

    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value=username))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(db, "db_get_restaurant_by_id",
                        AsyncMock(return_value=restaurant))

    # db_get_menu is only called as a fallback when restaurant.menu is empty/None
    monkeypatch.setattr(db, "db_get_menu", AsyncMock(return_value={}))

    return restaurant


# ── Test: authentication required ─────────────────────────────────────────────

class TestOnboardingAuth:
    def test_onboarding_requires_auth(self, client):
        """No auth header → 401 (verify_token returns None for any token)."""
        with patch("app.routes.deps.verify_token", AsyncMock(return_value=None)):
            resp = client.get(URL, headers={"Authorization": "Bearer invalid"})
        assert resp.status_code == 401


# ── Test: score = 100 (all steps done) ────────────────────────────────────────

class TestOnboardingFullScore:
    def test_onboarding_full_score(self, client, monkeypatch):
        """All checks pass → score == 100 and every step done."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="+573001234567",
            features={
                "billing_provider": "alegra",
            },
            menu={"Entradas": [{"name": "Ensalada", "price": 12000}]},
        )
        has_staff_mock, has_orders_mock = _make_repo_patches(has_staff=True, has_order=True)

        with patch(_PATCH_HAS_STAFF, has_staff_mock), \
             patch(_PATCH_HAS_ORDERS, has_orders_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["score"] == 100
        steps = body["steps"]
        assert steps["menu"]["done"] is True
        assert steps["staff"]["done"] is True
        assert steps["billing"]["done"] is True
        assert steps["whatsapp"]["done"] is True
        assert steps["first_order"]["done"] is True


# ── Test: partial score ────────────────────────────────────────────────────────

class TestOnboardingPartialScore:
    def test_onboarding_partial_score(self, client, monkeypatch):
        """
        menu=True, staff=True, billing=False, whatsapp=True, first_order=False
        → score == 60
        """
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="+573001234567",
            features={},   # no billing keys
            menu={"Platos": [{"name": "Burger", "price": 25000}]},
        )
        has_staff_mock, has_orders_mock = _make_repo_patches(has_staff=True, has_order=False)

        with patch(_PATCH_HAS_STAFF, has_staff_mock), \
             patch(_PATCH_HAS_ORDERS, has_orders_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        steps = body["steps"]
        assert steps["menu"]["done"] is True
        assert steps["staff"]["done"] is True
        assert steps["billing"]["done"] is False
        assert steps["whatsapp"]["done"] is True
        assert steps["first_order"]["done"] is False
        assert body["score"] == 60

    def test_only_whatsapp_done(self, client, monkeypatch):
        """Only whatsapp connected → score == 20."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="+573001234567",
            features={},
            menu=None,
        )
        has_staff_mock, has_orders_mock = _make_repo_patches(has_staff=False, has_order=False)

        with patch(_PATCH_HAS_STAFF, has_staff_mock), \
             patch(_PATCH_HAS_ORDERS, has_orders_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["score"] == 20
        assert body["steps"]["whatsapp"]["done"] is True

    def test_billing_alegra_email(self, client, monkeypatch):
        """billing step done when alegra_email present (not billing_provider)."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="",
            features={"alegra_email": "test@restaurante.com"},
            menu=None,
        )
        has_staff_mock, _ = _make_repo_patches(has_staff=False, has_order=False)

        with patch(_PATCH_HAS_STAFF, has_staff_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["steps"]["billing"]["done"] is True
        assert body["steps"]["whatsapp"]["done"] is False  # empty whatsapp_number

    def test_billing_enabled_flag(self, client, monkeypatch):
        """billing step done when billing_enabled=True."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="",
            features={"billing_enabled": True},
            menu=None,
        )
        has_staff_mock, _ = _make_repo_patches(has_staff=False, has_order=False)

        with patch(_PATCH_HAS_STAFF, has_staff_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        assert resp.json()["steps"]["billing"]["done"] is True


# ── Test: score = 0 (empty restaurant) ───────────────────────────────────────

class TestOnboardingEmptyRestaurant:
    def test_onboarding_empty_restaurant(self, client, monkeypatch):
        """No menu, no staff, no billing, no whatsapp, no orders → score == 0."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="",
            features={},
            menu=None,
        )
        # When whatsapp_number is empty, db_has_orders is never called (guarded by `if whatsapp_number`)
        has_staff_mock = AsyncMock(return_value=False)

        with patch(_PATCH_HAS_STAFF, has_staff_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["score"] == 0
        for key, step in body["steps"].items():
            assert step["done"] is False, f"Expected {key}.done=False, got True"

    def test_response_shape(self, client, monkeypatch):
        """Response must always have 'score' and 'steps' with all 5 step keys."""
        _patch_restaurant(monkeypatch)
        has_staff_mock, has_orders_mock = _make_repo_patches(has_staff=False, has_order=False)

        with patch(_PATCH_HAS_STAFF, has_staff_mock), \
             patch(_PATCH_HAS_ORDERS, has_orders_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert "score" in body
        assert "steps" in body
        for key in ("menu", "staff", "billing", "whatsapp", "first_order"):
            assert key in body["steps"]
            step = body["steps"][key]
            assert "done" in step
            assert "label" in step
            assert "description" in step

    def test_db_query_failure_defaults_to_false(self, client, monkeypatch):
        """If a repo function raises, the step is done=False and the endpoint still returns 200."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="+573001234567",
            features={},
            menu=None,
        )

        async def _raise(*_args, **_kwargs):
            raise Exception("DB down")

        with patch(_PATCH_HAS_STAFF, _raise), \
             patch(_PATCH_HAS_ORDERS, _raise):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["steps"]["staff"]["done"] is False
        assert body["steps"]["first_order"]["done"] is False
