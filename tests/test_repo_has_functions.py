"""
tests/test_repo_has_functions.py

Unit tests for the two new repo helpers introduced in the code-review followup:
  - staff_repo.db_has_staff(restaurant_id) -> bool
  - restaurant_repo.db_has_orders_by_bot_number(bot_number) -> bool

All tests are unit-level (no live DB). The pool is mocked via patch so the
suite runs in CI without a Postgres instance.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.tenant_context import tenant_scope as _tenant_scope


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pool(fetchval_result):
    """Build a minimal mock pool that returns fetchval_result from fetchval.
    conn.transaction() must be a sync callable returning an async ctx-mgr."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


_SHARED_POOL = "app.services.database.get_pool"
_REST_POOL   = "app.repositories.restaurant_repo._get_pool"


# ── db_has_staff ──────────────────────────────────────────────────────────────

class TestDbHasStaff:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_staff(self):
        from app.repositories.staff_repo import db_has_staff
        with patch(_SHARED_POOL, AsyncMock(return_value=_make_pool(False))):
            with _tenant_scope(42):
                result = await db_has_staff(restaurant_id=42)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_staff_exists(self):
        from app.repositories.staff_repo import db_has_staff
        with patch(_SHARED_POOL, AsyncMock(return_value=_make_pool(True))):
            with _tenant_scope(42):
                result = await db_has_staff(restaurant_id=42)
        assert result is True

    @pytest.mark.asyncio
    async def test_coerces_truthy_int_to_bool(self):
        """asyncpg may return 1 or True; both should coerce to True."""
        from app.repositories.staff_repo import db_has_staff
        with patch(_SHARED_POOL, AsyncMock(return_value=_make_pool(1))):
            with _tenant_scope(99):
                result = await db_has_staff(restaurant_id=99)
        assert result is True

    @pytest.mark.asyncio
    async def test_coerces_none_to_false(self):
        from app.repositories.staff_repo import db_has_staff
        with patch(_SHARED_POOL, AsyncMock(return_value=_make_pool(None))):
            with _tenant_scope(99):
                result = await db_has_staff(restaurant_id=99)
        assert result is False


# ── db_has_orders_by_bot_number ───────────────────────────────────────────────

class TestDbHasOrdersByBotNumber:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_orders(self):
        from app.repositories.restaurant_repo import db_has_orders_by_bot_number
        with patch(_REST_POOL, AsyncMock(return_value=_make_pool(False))):
            result = await db_has_orders_by_bot_number("+573001234567")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_orders_exist(self):
        from app.repositories.restaurant_repo import db_has_orders_by_bot_number
        with patch(_REST_POOL, AsyncMock(return_value=_make_pool(True))):
            result = await db_has_orders_by_bot_number("+573001234567")
        assert result is True

    @pytest.mark.asyncio
    async def test_coerces_truthy_int_to_bool(self):
        from app.repositories.restaurant_repo import db_has_orders_by_bot_number
        with patch(_REST_POOL, AsyncMock(return_value=_make_pool(1))):
            result = await db_has_orders_by_bot_number("+573009999999")
        assert result is True

    @pytest.mark.asyncio
    async def test_coerces_none_to_false(self):
        from app.repositories.restaurant_repo import db_has_orders_by_bot_number
        with patch(_REST_POOL, AsyncMock(return_value=_make_pool(None))):
            result = await db_has_orders_by_bot_number("+573009999999")
        assert result is False


# ── Onboarding endpoint smoke test (post-refactor) ────────────────────────────
# Verifies the endpoint still works correctly after SQL was moved to repos.
# Patches the two new repo functions directly instead of patching get_pool.

class TestOnboardingPostRefactor:
    def test_onboarding_calls_repo_functions(self, client, monkeypatch):
        """After refactor, onboarding endpoint delegates to repo helpers."""
        from app.services import database as db_mod

        restaurant = {
            "id":              1,
            "name":            "Test",
            "whatsapp_number": "+573001234567",
            "features":        {"billing_provider": "alegra"},
            "menu":            {"Entradas": [{"name": "Sopa", "price": 10000}]},
        }
        user = {
            "username":        "owner",
            "restaurant_name": "Test",
            "branch_id":       1,
            "role":            "owner",
            "password_hash":   "$2b$12$placeholder",
        }

        monkeypatch.setattr("app.routes.deps.verify_token",
                            AsyncMock(return_value="owner"))
        monkeypatch.setattr(db_mod, "db_get_user", AsyncMock(return_value=user))
        monkeypatch.setattr(db_mod, "db_get_restaurant_by_id",
                            AsyncMock(return_value=restaurant))
        monkeypatch.setattr(db_mod, "db_get_menu", AsyncMock(return_value={}))

        # Patch the repo functions that the route now calls
        monkeypatch.setattr(
            "app.routes.settings_routes.db_has_staff",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "app.routes.settings_routes.db_has_orders_by_bot_number",
            AsyncMock(return_value=True),
        )

        resp = client.get(
            "/api/onboarding/status",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["score"] == 100
        assert body["steps"]["staff"]["done"] is True
        assert body["steps"]["first_order"]["done"] is True
