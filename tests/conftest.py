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
    """Wrap a mock connection in a minimal pool context manager."""
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
