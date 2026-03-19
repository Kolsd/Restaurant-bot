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
    # 1. Burlamos la seguridad para que crea que somos un usuario válido
    monkeypatch.setattr(billing_routes, "verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(billing_routes.db, "db_get_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1}))
    
    # 2. Burlamos la configuración que viene de la base de datos
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
    # Verificamos que se censure el token de seguridad
    assert "fake_token_123" not in str(data["config"])
    assert "***" in str(data["config"])

def test_get_billing_config_unauthorized(client, monkeypatch):
    # Simulamos que el token no es válido
    monkeypatch.setattr(billing_routes, "verify_token", AsyncMock(return_value=None))
    
    response = client.get("/api/billing/config")
    
    assert response.status_code == 401
    assert response.json()["detail"] == "No autorizado"

def test_emit_manual_invoice_endpoint(client, monkeypatch):
    # Burlamos la seguridad nuevamente
    monkeypatch.setattr(billing_routes, "verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(billing_routes.db, "db_get_user", AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1}))

    # Burlamos la función que emite la factura a Alegra/Siigo
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

    # Verificamos que nuestro endpoint de la API llamó correctamente a la función de facturación
    mock_emit.assert_called_once()
    args, _ = mock_emit.call_args
    assert args[0] == "ORD-TEST-01"