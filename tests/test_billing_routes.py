import pytest
from unittest.mock import AsyncMock
import app.routes.billing as billing_routes

def test_get_providers_list(client):
    response = client.get("/api/billing/providers")
    assert response.status_code == 200
    data = response.json()
    assert "providers" in data
    provider_ids = [p["id"] for p in data["providers"]]
    assert "alegra" in provider_ids
    assert "siigo" in provider_ids
    assert "loggro" in provider_ids

def test_get_billing_config_authorized(client, monkeypatch):
    # 1. Bypass security so it believes we're a valid user
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr("app.routes.deps.db.db_get_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "org_id": 1}))

    # 2. Mock the config coming from the database
    mock_cfg = {
        "provider": "alegra",
        "alegra_email": "test@test.com",
        "alegra_token": "fake_token_123"
    }
    monkeypatch.setattr(billing_routes, "get_billing_config", AsyncMock(return_value=mock_cfg))

    headers = {"Authorization": "Bearer fake_token_123"}
    response = client.get("/api/billing/config", headers=headers)
    
    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is True
    # Verify the security token is redacted
    assert "fake_token_123" not in str(data["config"])
    assert "***" in str(data["config"])

def test_get_billing_config_unauthorized(client, monkeypatch):
    # Simulate an invalid token
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=None))

    response = client.get("/api/billing/config")

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"

def test_emit_manual_invoice_endpoint(client, monkeypatch):
    # Bypass security again
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr("app.routes.deps.db.db_get_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1, "org_id": 1}))

    # Mock restaurant lookup with dian_enabled=True (gate check added 2026-05-07)
    mock_restaurant = {"id": 1, "name": "Test", "features": {"dian_enabled": True}}
    import app.routes.billing as _billing_routes_mod
    monkeypatch.setattr(_billing_routes_mod.db, "db_get_restaurant_by_org_id", AsyncMock(return_value=mock_restaurant))

    # Mock the function that issues the invoice to Alegra/Siigo
    mock_emit = AsyncMock(return_value={"success": True, "provider": "alegra", "external_id": "999"})
    monkeypatch.setattr(billing_routes, "emit_invoice", mock_emit)

    headers = {"Authorization": "Bearer fake_token_123"}
    payload = {
        "order_id": "ORD-TEST-01",
        "customer": {"name": "Test Cliente"}
    }

    response = client.post("/api/billing/emit", json=payload, headers=headers)
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["external_id"] == "999"

    # Verify our API endpoint correctly called the billing function
    mock_emit.assert_called_once()
    args, _ = mock_emit.call_args
    assert args[0] == "ORD-TEST-01"