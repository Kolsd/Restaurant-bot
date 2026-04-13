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

Each test patches get_pool in the settings_routes namespace for the two
DB queries (staff count and first-order existence check).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import database as db

# Target for pool patches inside the endpoint module
_PATCH_POOL = "app.routes.settings_routes.get_pool"

URL = "/api/onboarding/status"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pool(staff_count: int = 0, has_order: bool = False):
    """
    Build a mock pool whose connections answer the two fetchval calls made
    by the endpoint: staff COUNT and orders EXISTS.
    The pool is called twice (once per query), so side_effect is a list.
    """
    def _conn(return_value):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=return_value)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.acquire = MagicMock(return_value=cm)
        return pool

    # get_pool is called once for staff, once for first_order
    get_pool_mock = AsyncMock(side_effect=[
        _conn(staff_count),   # staff COUNT(*)
        _conn(has_order),     # orders EXISTS(...)
    ])
    return get_pool_mock


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
        get_pool_mock = _make_pool(staff_count=3, has_order=True)

        with patch(_PATCH_POOL, get_pool_mock):
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
        # staff_count=2 (has staff), has_order=False (no first order)
        get_pool_mock = _make_pool(staff_count=2, has_order=False)

        with patch(_PATCH_POOL, get_pool_mock):
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
        # staff_count=0, has_order=False
        get_pool_mock = _make_pool(staff_count=0, has_order=False)

        with patch(_PATCH_POOL, get_pool_mock):
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
        get_pool_mock = _make_pool(staff_count=0, has_order=False)

        with patch(_PATCH_POOL, get_pool_mock):
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
        get_pool_mock = _make_pool(staff_count=0, has_order=False)

        with patch(_PATCH_POOL, get_pool_mock):
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
        # When whatsapp_number is empty, get_pool is called once (staff only);
        # first_order query is skipped.
        def _conn_zero():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=0)
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=conn)
            cm.__aexit__ = AsyncMock(return_value=False)
            pool = AsyncMock()
            pool.acquire = MagicMock(return_value=cm)
            return pool

        get_pool_mock = AsyncMock(return_value=_conn_zero())

        with patch(_PATCH_POOL, get_pool_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["score"] == 0
        for key, step in body["steps"].items():
            assert step["done"] is False, f"Expected {key}.done=False, got True"

    def test_response_shape(self, client, monkeypatch):
        """Response must always have 'score' and 'steps' with all 5 step keys."""
        _patch_restaurant(monkeypatch)
        get_pool_mock = _make_pool(staff_count=0, has_order=False)

        with patch(_PATCH_POOL, get_pool_mock):
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
        """If a DB query raises, the step is done=False and the endpoint still returns 200."""
        _patch_restaurant(
            monkeypatch,
            whatsapp_number="+573001234567",
            features={},
            menu=None,
        )

        # Both pool calls raise an exception
        def _error_pool():
            pool = AsyncMock()
            pool.acquire = MagicMock(side_effect=Exception("DB down"))
            return pool

        get_pool_mock = AsyncMock(side_effect=[_error_pool(), _error_pool()])

        with patch(_PATCH_POOL, get_pool_mock):
            resp = client.get(URL, headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["steps"]["staff"]["done"] is False
        assert body["steps"]["first_order"]["done"] is False
