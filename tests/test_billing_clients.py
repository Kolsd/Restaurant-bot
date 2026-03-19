import pytest
import respx
from httpx import Response
from app.services.billing import AlegraClient, SiigoClient, LoggroClient

# Usamos la marca "asyncio" porque tu código usa async/await
@pytest.mark.asyncio
@respx.mock
async def test_alegra_create_invoice():
    # 1. MOCK: Interceptamos la llamada a la API de Alegra
    respx.post("https://app.alegra.com/api/v1/invoices").mock(
        return_value=Response(200, json={"id": 888, "status": "success"})
    )

    # 2. Preparamos nuestro cliente y datos falsos
    client = AlegraClient("test@mesio.com", "token_falso")
    
    mock_order = {
        "id": "ORD-TEST-01",
        "total": 45000,
        "items": [{"name": "Hamburguesa", "price": 45000, "quantity": 1}],
        "customer": {"alegra_id": "10"}
    }
    
    mock_config = {
        "item_id_default": 1,
        "payment_type_id": 1,
        "currency": "COP"
    }

    # 3. Ejecutamos la función de tu código
    result = await client.create_invoice(mock_order, mock_config)

    # 4. AFIRMAMOS (Assert) que el resultado sea el esperado
    assert result["id"] == 888
    assert result["status"] == "success"

@pytest.mark.asyncio
@respx.mock
async def test_siigo_create_invoice():
    # 1. MOCK: Siigo requiere primero un token, y luego crear la factura. Mockeamos ambas rutas.
    respx.post("https://siigo.com/api/auth").mock(
        return_value=Response(200, json={"access_token": "jwt_falso_123"})
    )
    respx.post("https://siigo.com/api/v1/invoices").mock(
        return_value=Response(200, json={"id": "SIIGO-001", "name": "FV-1"})
    )

    client = SiigoClient("user_test", "key_test")
    
    mock_order = {
        "id": "ORD-TEST-02",
        "total": 30000,
        "items": [{"name": "Pizza", "price": 30000, "quantity": 1}],
    }
    
    mock_config = {
        "document_id": "1",
        "tax_id": "2",
        "payment_id": "5765",
        "product_code": "CONS001",
        "iva_percentage": 19
    }

    result = await client.create_invoice(mock_order, mock_config)

    # Verificamos que funcionó
    assert result["id"] == "SIIGO-001"

@pytest.mark.asyncio
@respx.mock
async def test_loggro_create_invoice():
    # 1. MOCK: Interceptamos Loggro
    respx.post("https://api.loggro.com/api/v1/invoices").mock(
        return_value=Response(200, json={"invoiceId": "LOG-999", "state": "approved"})
    )

    client = LoggroClient("api_key_falsa", "company_falsa")
    
    mock_order = {
        "id": "ORD-TEST-03",
        "total": 15000,
        "items": [{"name": "Bebida", "price": 15000, "quantity": 1}]
    }
    
    mock_config = {
        "resolution_id": "RES-123",
        "payment_method_code": "CASH",
        "product_code_default": "P-01",
        "customer_nit_default": "222222222"
    }

    result = await client.create_invoice(mock_order, mock_config)

    assert result["invoiceId"] == "LOG-999"