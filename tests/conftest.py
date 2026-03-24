import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services import database as db

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def mock_db(monkeypatch):
    async def mock_get_billing_config(restaurant_id: int):
        return {
            "provider": "alegra",
            "alegra_email": "test@test.com",
            "alegra_token": "fake_token_123",
            "item_id_default": "1",
            "payment_type_id": "1",
            "default_customer_id": "1",
            "currency": "COP"
        }

    async def mock_get_order(order_id: str):
        return {
            "id": order_id,
            "order_type": "domicilio",
            "total": 35000,
            "items": [{"name": "Pizza Margherita", "price": 35000, "quantity": 1}],
            "customer": {"alegra_id": "123"}
        }

    async def mock_get_table_bill(base_order_id: str):
        return {
            "base_order_id": base_order_id,
            "total": 50000,
            "sub_orders": [
                {"items": [{"name": "Pasta", "quantity": 1, "price": 25000}]},
                {"items": [{"name": "Vino", "quantity": 1, "price": 25000}]}
            ]
        }

    async def mock_log_billing_event(*args, **kwargs):
        pass

    async def mock_verify_token(token: str):
        return "admin_test"

    async def mock_get_user(username: str):
        return {"username": "admin_test", "restaurant_name": "Test Rest", "branch_id": 1, "role": "owner"}

    monkeypatch.setattr(db, "db_get_order", mock_get_order)
    monkeypatch.setattr(db, "db_get_table_bill", mock_get_table_bill)
    monkeypatch.setattr("app.services.billing.get_billing_config", mock_get_billing_config)
    monkeypatch.setattr("app.services.billing.log_billing_event", mock_log_billing_event)
    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db, "db_get_user", mock_get_user)