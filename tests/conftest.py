"""
Mesio Global Test Configuration
tests/conftest.py

Provides shared fixtures used across all test suites.
No live database or external API calls are made — everything is mocked.

Fixtures:
  client              — TestClient wrapping app (sync)
  mock_db             — legacy monkeypatch fixture kept for existing tests
  auth_override       — (module-level) sets app.dependency_overrides for auth deps
  make_pool / make_row — DB connection factory helpers (async tests)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.services import database as db


# ── Basic client ─────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


# ── Superadmin session mock (used by analytics, health metrics, ops tests) ────

@pytest.fixture
def mock_superadmin_session():
    """Patch sessions_repo.get_session so any non-empty token returns 'superadmin'.

    Use this fixture in tests for endpoints guarded by verify_superadmin.
    The fixture patches the canonical location where verify_superadmin resolves it.
    """
    from unittest.mock import AsyncMock, patch

    async def _get_session(token):
        return "superadmin" if token else None

    with patch("app.repositories.sessions_repo.get_session", AsyncMock(side_effect=_get_session)):
        yield


# ── Clear rate limit state between tests ─────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """Reset in-process rate limit fallback dict so tests don't bleed into each other."""
    from app.services import state_store
    state_store._fb_rate_limits.clear()
    yield
    state_store._fb_rate_limits.clear()


# ── DB row / pool factory helpers (reused across async suites) ───────────────

def make_row(d: dict):
    """Build an object that mimics an asyncpg Record."""
    row = MagicMock()
    row.__iter__ = lambda s: iter(d.items())
    row.keys     = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get      = lambda k, default=None: d.get(k, default)
    return row


def make_pool(conn):
    """Wrap a mock connection in a minimal pool context manager.

    Also ensures conn.transaction() is a sync callable returning an async ctx-mgr,
    as required by tenant_connection() which calls `async with conn.transaction():`.
    If conn already has transaction set to a MagicMock, it is left unchanged.
    """
    # Ensure transaction() is a sync MagicMock returning an async ctx-mgr.
    # AsyncMock's auto-generated attributes return coroutines when called, which
    # breaks `async with conn.transaction():`. Only patch if not already correct.
    if not isinstance(getattr(conn, "transaction", None), MagicMock):
        _txn = MagicMock()
        _txn.__aenter__ = AsyncMock(return_value=_txn)
        _txn.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=_txn)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))
    return pool


# ── Auth helpers (monkeypatch shortcuts) ─────────────────────────────────────

def patch_auth(monkeypatch, *, restaurant_id: int = 1,
               whatsapp_number: str = "+573001234567",
               features: dict = None,
               username: str = "owner_test",
               role: str = "owner"):
    """
    Shortcut: patch verify_token + db_get_user + db_get_restaurant_by_id so that
    any Bearer token is accepted and the given restaurant dict is returned.

    Returns the restaurant dict so callers can assert against it.
    """
    if features is None:
        features = {}

    restaurant = {
        "id":               restaurant_id,
        "name":             "Restaurante Test",
        "whatsapp_number":  whatsapp_number,
        "features":         features,
    }
    user = {
        "username":         username,
        "restaurant_name":  "Restaurante Test",
        "branch_id":        restaurant_id,
        "role":             role,
        "password_hash":    "$2b$12$placeholder",
    }

    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value=username))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(db, "db_get_restaurant_by_id",
                        AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_check_module",
                        AsyncMock(return_value=False))

    return restaurant


# ── Legacy mock_db fixture (kept for test_billing_routes.py) ─────────────────

@pytest.fixture
def mock_db(monkeypatch):
    async def mock_get_billing_config(restaurant_id: int):
        return {
            "provider":            "alegra",
            "alegra_email":        "test@test.com",
            "alegra_token":        "fake_token_123",
            "item_id_default":     "1",
            "payment_type_id":     "1",
            "default_customer_id": "1",
            "currency":            "COP",
        }

    async def mock_get_order(order_id: str):
        return {
            "id":         order_id,
            "order_type": "domicilio",
            "total":      35000,
            "items":      [{"name": "Pizza Margherita", "price": 35000, "quantity": 1}],
            "customer":   {"alegra_id": "123"},
        }

    async def mock_get_table_bill(base_order_id: str):
        return {
            "base_order_id": base_order_id,
            "total":         50000,
            "sub_orders": [
                {"items": [{"name": "Pasta", "quantity": 1, "price": 25000}]},
                {"items": [{"name": "Vino",  "quantity": 1, "price": 25000}]},
            ],
        }

    async def mock_log_billing_event(*args, **kwargs):
        pass

    async def mock_verify_token(token: str):
        return "admin_test"

    async def mock_get_user(username: str):
        return {
            "username":        "admin_test",
            "restaurant_name": "Test Rest",
            "branch_id":       1,
            "role":            "owner",
        }

    monkeypatch.setattr(db, "db_get_order",         mock_get_order)
    monkeypatch.setattr(db, "db_get_table_bill",    mock_get_table_bill)
    monkeypatch.setattr("app.services.billing.get_billing_config",  mock_get_billing_config)
    monkeypatch.setattr("app.services.billing.log_billing_event",   mock_log_billing_event)
    monkeypatch.setattr("app.routes.deps.verify_token",             mock_verify_token)
    monkeypatch.setattr(db, "db_get_user",          mock_get_user)


# ── Real PostgreSQL fixtures (integration tests) ──────────────────────────────

import asyncpg
import os


@pytest.fixture
async def db_pool():
    """Real PostgreSQL connection pool for integration tests.

    Reads TEST_DATABASE_URL first, falls back to DATABASE_URL.
    Skips the test if neither is set so the suite stays green in CI
    without a database.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("No DATABASE_URL or TEST_DATABASE_URL set — skipping integration test")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    yield pool
    await pool.close()


@pytest.fixture
async def db_conn(db_pool):
    """Single connection wrapped in a transaction that is always rolled back.

    This gives each test a clean slate without truncating or seeding tables:
    the transaction is started before the test body runs and rolled back once
    it finishes, regardless of whether the test passed or failed.
    """
    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            yield conn
        finally:
            await tx.rollback()  # Always rollback — test isolation
