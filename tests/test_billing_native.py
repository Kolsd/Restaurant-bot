"""
Tests para el Motor de Facturación Nativa DIAN (MesioNativeAdapter).
Cubre: CUFE, CUDS, XML UBL 2.1, create_invoice con mocks de DB, test_connection.
No requiere base de datos ni credenciales reales.
"""
import pytest
from unittest.mock import AsyncMock, patch
from datetime import date

from app.services.billing import MesioNativeAdapter, get_adapter


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def adapter():
    return MesioNativeAdapter()


MOCK_RESOLUTION = {
    "id":                1,
    "restaurant_id":     42,
    "resolution_number": "18764006352289",
    "resolution_date":   "2019-01-19",
    "prefix":            "FE",
    "from_number":       990000000,
    "to_number":         995000000,
    "valid_from":        "2019-01-19",
    "valid_to":          "2030-12-31",
    "technical_key":     "fc8eac422eba16e22ffd8c6f94b3f40a6e38162c",
    "current_number":    0,
    "environment":       "test",
    "software_id":       "software-uuid-test",
    "software_pin":      "12345",
    "updated_at":        "2024-01-01T00:00:00",
}

MOCK_CONFIG = {
    "_restaurant_id":        42,
    "provider":              "mesio_native",
    "restaurant_nit":        "700085462",
    "restaurant_legal_name": "Restaurante El Buen Sabor SAS",
    "restaurant_city_code":  "11001",
    "restaurant_city_name":  "Bogotá",
    "restaurant_address_dian": "Calle 100 #15-20",
    "tax_regime":            "iva",
    "tax_percentage":        19.0,
    "nit_id_type":           "31",
    "software_id":           "software-uuid-test",
    "software_pin":          "12345",
    "dian_environment":      "test",
    "currency":              "COP",
}

MOCK_ORDER = {
    "id":         "MESA-ABC123",
    "order_type": "mesa",
    "total":      119000,
    "items": [
        {"name": "Pizza Margherita", "price": 95000, "quantity": 1},
        {"name": "Gaseosa",          "price": 5000,  "quantity": 2},
    ],
    "payment_method": "cash",
}


# ══════════════════════════════════════════════════════════════════════
# 1. CUFE — algoritmo SHA-384
# ══════════════════════════════════════════════════════════════════════

def test_cufe_longitud_correcta(adapter):
    """El CUFE debe ser exactamente 96 caracteres hexadecimales (SHA-384)."""
    cufe = adapter._calcular_cufe(
        num_fac="SETP990000001", fec_fac="2019-09-10",
        hor_fac="00:31:40-05:00", val_fac="1000000.00",
        val_imp1="190000.00", val_imp2="0.00", val_tot="1190000.00",
        nit_ofe="700085462", num_adq="800199436",
        cl_tec="fc8eac422eba16e22ffd8c6f94b3f40a6e38162c",
    )
    assert len(cufe) == 96
    assert all(c in "0123456789abcdef" for c in cufe)


def test_cufe_determinista(adapter):
    """El mismo input siempre produce el mismo CUFE."""
    kwargs = dict(
        num_fac="FE-1001", fec_fac="2024-06-15",
        hor_fac="12:00:00-05:00", val_fac="50000.00",
        val_imp1="9500.00", val_imp2="0.00", val_tot="59500.00",
        nit_ofe="900123456", num_adq="222222222",
        cl_tec="clave-tecnica-test",
    )
    assert adapter._calcular_cufe(**kwargs) == adapter._calcular_cufe(**kwargs)


def test_cufe_cambia_con_numero_diferente(adapter):
    """Dos facturas con diferente número deben tener CUFE diferente."""
    base = dict(
        fec_fac="2024-06-15", hor_fac="12:00:00-05:00",
        val_fac="50000.00", val_imp1="9500.00",
        val_imp2="0.00", val_tot="59500.00",
        nit_ofe="900123456", num_adq="222222222",
        cl_tec="clave-tecnica",
    )
    cufe1 = adapter._calcular_cufe(num_fac="FE-1001", **base)
    cufe2 = adapter._calcular_cufe(num_fac="FE-1002", **base)
    assert cufe1 != cufe2


# ══════════════════════════════════════════════════════════════════════
# 2. CUDS — código de software
# ══════════════════════════════════════════════════════════════════════

def test_cuds_longitud_correcta(adapter):
    cuds = adapter._calcular_cuds("soft-id-abc", "pin999", "900123456")
    assert len(cuds) == 96


def test_cuds_cambia_con_pin_diferente(adapter):
    cuds1 = adapter._calcular_cuds("same-id", "pin-a", "900123456")
    cuds2 = adapter._calcular_cuds("same-id", "pin-b", "900123456")
    assert cuds1 != cuds2


# ══════════════════════════════════════════════════════════════════════
# 3. XML UBL 2.1 — estructura básica
# ══════════════════════════════════════════════════════════════════════

def test_xml_contiene_elementos_obligatorios(adapter):
    """El XML generado debe incluir los elementos UBL 2.1 requeridos por la DIAN."""
    xml = adapter._build_ubl_xml(
        invoice_number="FE-1001",
        issue_date="2024-06-15",
        issue_time="12:00:00",
        cufe="a" * 96,
        cuds="b" * 96,
        qr_url="https://catalogo-vpfe-hab.dian.gov.co/document/searchqr?documentkey=" + "a" * 96,
        resolution=MOCK_RESOLUTION,
        config=MOCK_CONFIG,
        order=MOCK_ORDER,
        subtotal_cents=10000000,
        tax_cents=1900000,
        total_cents=11900000,
        tax_regime="iva",
        tax_pct=19.0,
        customer={"nit": "222222222", "name": "Consumidor Final", "email": "", "id_type": "13"},
        environment_id="2",
    )
    assert '<?xml version="1.0"' in xml
    assert "UBL 2.1" in xml
    assert "CUFE-SHA384" in xml
    assert "FE-1001" in xml
    assert "2024-06-15" in xml
    assert "18764006352289" in xml          # Número de resolución
    assert "700085462" in xml               # NIT emisor
    assert "222222222" in xml               # NIT cliente genérico
    assert "Pizza Margherita" in xml
    assert "Gaseosa" in xml
    assert "01" in xml                      # Código IVA
    assert "IVA" in xml


def test_xml_ico_usa_esquema_04(adapter):
    """Para régimen ICO el XML debe usar código de impuesto '04' y nombre 'INC'."""
    xml = adapter._build_ubl_xml(
        invoice_number="FE-2001",
        issue_date="2024-06-15",
        issue_time="12:00:00",
        cufe="c" * 96,
        cuds="d" * 96,
        qr_url="https://example.com/qr",
        resolution=MOCK_RESOLUTION,
        config={**MOCK_CONFIG, "tax_regime": "ico"},
        order=MOCK_ORDER,
        subtotal_cents=10000000,
        tax_cents=800000,
        total_cents=10800000,
        tax_regime="ico",
        tax_pct=8.0,
        customer={"nit": "222222222", "name": "Consumidor Final", "email": "", "id_type": "13"},
        environment_id="2",
    )
    assert "INC" in xml
    assert "<cbc:ID>04</cbc:ID>" in xml


def test_xml_escapa_caracteres_especiales(adapter):
    """Nombres con caracteres XML especiales no deben romper el documento."""
    order_con_ampersand = {
        **MOCK_ORDER,
        "items": [{"name": "Pollo & Papas <fritas>", "price": 25000, "quantity": 1}],
    }
    xml = adapter._build_ubl_xml(
        invoice_number="FE-3001",
        issue_date="2024-06-15",
        issue_time="12:00:00",
        cufe="e" * 96,
        cuds="f" * 96,
        qr_url="https://example.com/qr",
        resolution=MOCK_RESOLUTION,
        config=MOCK_CONFIG,
        order=order_con_ampersand,
        subtotal_cents=2100840,
        tax_cents=399160,
        total_cents=2500000,
        tax_regime="iva",
        tax_pct=19.0,
        customer={"nit": "222222222", "name": "Consumidor Final", "email": "", "id_type": "13"},
        environment_id="2",
    )
    assert "&amp;" in xml
    assert "&lt;" in xml
    assert "&gt;" in xml
    # No debe haber & o < sin escapar dentro de contenido de elemento
    assert "Pollo &amp; Papas &lt;fritas&gt;" in xml


# ══════════════════════════════════════════════════════════════════════
# 4. create_invoice — flujo completo con mocks de DB
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_invoice_flujo_completo(adapter):
    """create_invoice debe devolver CUFE, número de factura y persistir en DB."""
    with (
        patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=MOCK_RESOLUTION)),
        patch("app.services.billing.db.db_claim_next_invoice_number", new=AsyncMock(return_value=990000001)),
        patch("app.services.billing.db.db_save_fiscal_invoice", new=AsyncMock(return_value=1)),
    ):
        result = await adapter.create_invoice(MOCK_ORDER, MOCK_CONFIG)

    assert result["invoice_number"] == "FE990000001"
    assert len(result["cufe"]) == 96
    assert result["dian_status"] == "draft"
    assert result["xml_available"] is True
    # Verificar cálculo de impuesto IVA 19% sobre precio que ya incluye impuesto
    # total=119000 → subtotal=100000 (aprox), iva=19000
    assert abs(result["total"] - 119000) < 1
    assert result["tax_pct"] == 19.0


@pytest.mark.asyncio
async def test_create_invoice_sin_restaurant_id_lanza_error(adapter):
    """Si _restaurant_id no está en config debe lanzar ValueError."""
    config_sin_id = {k: v for k, v in MOCK_CONFIG.items() if k != "_restaurant_id"}
    with pytest.raises(ValueError, match="_restaurant_id"):
        await adapter.create_invoice(MOCK_ORDER, config_sin_id)


@pytest.mark.asyncio
async def test_create_invoice_sin_resolucion_lanza_error(adapter):
    """Si no hay resolución DIAN configurada debe lanzar RuntimeError descriptivo."""
    with patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="resolución DIAN"):
            await adapter.create_invoice(MOCK_ORDER, MOCK_CONFIG)


@pytest.mark.asyncio
async def test_create_invoice_resolucion_vencida(adapter):
    """Una resolución cuya valid_to es pasada debe rechazarse."""
    resolucion_vencida = {**MOCK_RESOLUTION, "valid_to": "2020-01-01"}
    with patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=resolucion_vencida)):
        with pytest.raises(RuntimeError, match="venció"):
            await adapter.create_invoice(MOCK_ORDER, MOCK_CONFIG)


@pytest.mark.asyncio
async def test_create_invoice_ico_calcula_8_pct(adapter):
    """Con tax_regime='ico' debe aplicar 8% de Impuesto al Consumo."""
    config_ico = {**MOCK_CONFIG, "tax_regime": "ico", "tax_percentage": 8.0}
    order_ico  = {**MOCK_ORDER, "total": 108000}  # 100000 base + 8% = 108000

    with (
        patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=MOCK_RESOLUTION)),
        patch("app.services.billing.db.db_claim_next_invoice_number", new=AsyncMock(return_value=990000002)),
        patch("app.services.billing.db.db_save_fiscal_invoice", new=AsyncMock(return_value=2)),
    ):
        result = await adapter.create_invoice(order_ico, config_ico)

    assert result["tax_regime"] == "ico"
    assert result["tax_pct"] == 8.0
    # subtotal ≈ 100000, impuesto ≈ 8000
    assert abs(result["tax"] - 8000) < 2


@pytest.mark.asyncio
async def test_db_save_fiscal_invoice_se_llama_con_cufe(adapter):
    """Verifica que db_save_fiscal_invoice recibe el CUFE correcto (no vacío)."""
    mock_save = AsyncMock(return_value=99)
    with (
        patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=MOCK_RESOLUTION)),
        patch("app.services.billing.db.db_claim_next_invoice_number", new=AsyncMock(return_value=990000003)),
        patch("app.services.billing.db.db_save_fiscal_invoice", new=mock_save),
    ):
        await adapter.create_invoice(MOCK_ORDER, MOCK_CONFIG)

    call_args = mock_save.call_args[0][0]
    assert len(call_args["cufe"]) == 96
    assert call_args["dian_status"] == "draft"
    assert call_args["restaurant_id"] == 42
    assert call_args["order_id"] == "MESA-ABC123"


# ══════════════════════════════════════════════════════════════════════
# 5. test_connection
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_connection_config_valida(adapter):
    """Con config completa y resolución vigente debe retornar status ok."""
    with patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=MOCK_RESOLUTION)):
        result = await adapter.test_connection(MOCK_CONFIG)

    assert result["sample"][0]["config_ok"] is True
    assert result["sample"][0]["resolution"]["expired"] is False


@pytest.mark.asyncio
async def test_connection_falta_campo_obligatorio(adapter):
    """Sin restaurant_nit debe lanzar ValueError con el campo faltante."""
    config_incompleto = {k: v for k, v in MOCK_CONFIG.items() if k != "restaurant_nit"}
    with pytest.raises(ValueError, match="restaurant_nit"):
        await adapter.test_connection(config_incompleto)


@pytest.mark.asyncio
async def test_connection_resolucion_vencida(adapter):
    """test_connection debe informar que la resolución está vencida."""
    res_vencida = {**MOCK_RESOLUTION, "valid_to": "2020-06-01"}
    with patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=res_vencida)):
        with pytest.raises(RuntimeError, match="Resolución DIAN vencida"):
            await adapter.test_connection(MOCK_CONFIG)


@pytest.mark.asyncio
async def test_connection_sin_resolucion_informa(adapter):
    """Sin resolución en DB test_connection debe devolver status sin error fatal."""
    with patch("app.services.billing.db.db_get_fiscal_resolution", new=AsyncMock(return_value=None)):
        result = await adapter.test_connection(MOCK_CONFIG)
    assert "No configurada" in str(result["sample"][0]["resolution"])


# ══════════════════════════════════════════════════════════════════════
# 6. Adapter Pattern — integración con get_adapter y emit_invoice
# ══════════════════════════════════════════════════════════════════════

def test_providers_list_incluye_mesio_native(client):
    """El endpoint /providers debe exponer mesio_native."""
    response = client.get("/api/billing/providers")
    assert response.status_code == 200
    ids = [p["id"] for p in response.json()["providers"]]
    assert "mesio_native" in ids
    # Los proveedores existentes no deben desaparecer
    assert "siigo" in ids
    assert "alegra" in ids
    assert "loggro" in ids


def test_set_config_acepta_mesio_native(client, monkeypatch):
    """POST /api/billing/config debe aceptar provider=mesio_native."""
    from unittest.mock import AsyncMock
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(
        "app.routes.deps.db.db_get_user",
        AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1}),
    )
    monkeypatch.setattr("app.routes.billing.save_billing_config", AsyncMock())

    headers  = {"Authorization": "Bearer token"}
    payload  = {
        "provider":              "mesio_native",
        "restaurant_nit":        "900123456",
        "restaurant_legal_name": "Test SAS",
        "restaurant_city_code":  "11001",
        "restaurant_city_name":  "Bogotá",
        "restaurant_address_dian": "Calle 1 #2-3",
        "tax_regime":            "iva",
        "software_id":           "soft-uuid",
        "software_pin":          "pin123",
        "dian_environment":      "test",
    }
    response = client.post("/api/billing/config", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["provider"] == "mesio_native"


def test_get_billing_config_not_configured(client, monkeypatch):
    """GET /api/billing/config sin config devuelve configured=False."""
    from unittest.mock import AsyncMock
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="admin_test"))
    monkeypatch.setattr(
        "app.routes.deps.db.db_get_user",
        AsyncMock(return_value={"username": "admin", "restaurant_name": "Test", "branch_id": 1}),
    )
    monkeypatch.setattr("app.routes.billing.get_billing_config", AsyncMock(return_value=None))

    headers  = {"Authorization": "Bearer token"}
    response = client.get("/api/billing/config", headers=headers)
    assert response.status_code == 200
    assert response.json()["configured"] is False
