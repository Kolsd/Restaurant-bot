"""
Mesio — Rutas de Billing / Facturación
"""

import json
import os
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services.auth import verify_token
from app.services import database as db
from app.services.billing import (
    get_billing_config,
    save_billing_config,
    get_billing_log,
    emit_invoice,
    SiigoClient,
    AlegraClient,
    LoggroClient,
)

router = APIRouter(prefix="/api/billing", tags=["billing"])


# ── AUTH HELPER ───────────────────────────────────────────────────────

async def _require_auth(request: Request) -> dict:
    token    = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="No autorizado")
    user = await db.db_get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    return user

async def _get_restaurant_id(user: dict) -> int:
    if user.get("branch_id"):
        return user["branch_id"]
    all_r = await db.db_get_all_restaurants()
    for r in all_r:
        if r["name"].lower().strip() == user["restaurant_name"].lower().strip():
            return r["id"]
    if all_r:
        return all_r[0]["id"]
    raise HTTPException(status_code=404, detail="Restaurante no encontrado")


# ── MODELOS ──────────────────────────────────────────────────────────

class BillingConfigPayload(BaseModel):
    provider:    str  # "siigo" | "alegra" | "loggro"
    auto_emit:   bool = False
    # Siigo
    siigo_username:      Optional[str] = None
    siigo_access_key:    Optional[str] = None
    document_id:         Optional[str] = None
    seller_id:           Optional[str] = None
    tax_id:              Optional[str] = None
    payment_id:          Optional[str] = None
    product_code:        Optional[str] = None
    default_customer_nit: Optional[str] = None
    # Alegra
    alegra_email:        Optional[str] = None
    alegra_token:        Optional[str] = None
    item_id_default:     Optional[str] = None
    payment_type_id:     Optional[str] = None
    warehouse_id:        Optional[str] = None
    iva_id:              Optional[str] = None
    default_customer_id: Optional[str] = None
    # Loggro
    loggro_api_key:      Optional[str] = None
    loggro_company_id:   Optional[str] = None
    resolution_id:       Optional[str] = None
    payment_method_code: Optional[str] = None
    product_code_default: Optional[str] = None
    customer_nit_default: Optional[str] = None
    # Compartidos
    iva_percentage:      Optional[float] = 0
    currency:            Optional[str]   = "COP"

class EmitInvoicePayload(BaseModel):
    order_id:  str
    customer:  Optional[dict] = None  # {nit, name, email, alegra_id}

class AdminConfigPayload(BaseModel):
    admin_key:     str
    restaurant_id: int
    config:        dict


# ── ENDPOINTS ────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request):
    user          = await _require_auth(request)
    restaurant_id = await _get_restaurant_id(user)
    config        = await get_billing_config(restaurant_id)
    if not config:
        return {"configured": False}
    # Ocultar secretos en la respuesta
    safe = {k: ("***" if "key" in k.lower() or "token" in k.lower() or "password" in k.lower() or "secret" in k.lower() else v)
            for k, v in config.items()}
    return {"configured": True, "config": safe}


@router.post("/config")
async def set_config(request: Request, payload: BillingConfigPayload):
    user          = await _require_auth(request)
    restaurant_id = await _get_restaurant_id(user)

    allowed = {"siigo", "alegra", "loggro"}
    if payload.provider.lower() not in allowed:
        raise HTTPException(status_code=400, detail=f"Proveedor debe ser uno de: {allowed}")

    config = payload.model_dump(exclude_none=True)
    await save_billing_config(restaurant_id, config)
    return {"success": True, "provider": payload.provider}


@router.post("/emit")
async def emit(request: Request, payload: EmitInvoicePayload):
    """Emite manualmente una factura para un pedido específico."""
    user          = await _require_auth(request)
    restaurant_id = await _get_restaurant_id(user)
    result        = await emit_invoice(payload.order_id, restaurant_id, payload.customer)
    if not result["success"]:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.get("/log")
async def billing_log(request: Request, limit: int = 50):
    user          = await _require_auth(request)
    restaurant_id = await _get_restaurant_id(user)
    log           = await get_billing_log(restaurant_id, limit)
    return {"log": log}


@router.post("/test-connection")
async def test_connection(request: Request):
    """Prueba las credenciales sin emitir factura real."""
    user          = await _require_auth(request)
    restaurant_id = await _get_restaurant_id(user)
    config        = await get_billing_config(restaurant_id)

    if not config:
        raise HTTPException(status_code=400, detail="Billing no configurado")

    provider = config.get("provider", "").lower()
    try:
        if provider == "siigo":
            client  = SiigoClient(config["siigo_username"], config["siigo_access_key"])
            result  = await client.get_document_types()
            return {"success": True, "provider": "siigo", "sample": result[:2]}

        elif provider == "alegra":
            client  = AlegraClient(config["alegra_email"], config["alegra_token"])
            result  = await client.get_payment_methods()
            return {"success": True, "provider": "alegra", "sample": result[:2]}

        elif provider == "loggro":
            client  = LoggroClient(config["loggro_api_key"], config["loggro_company_id"])
            result  = await client.get_products()
            return {"success": True, "provider": "loggro", "sample": result[:2]}

        else:
            raise HTTPException(status_code=400, detail=f"Proveedor '{provider}' no soportado")

    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/providers")
async def list_providers():
    """Devuelve los proveedores soportados con sus campos requeridos."""
    return {
        "providers": [
            {
                "id": "siigo",
                "name": "Siigo",
                "logo": "https://www.siigo.com/favicon.ico",
                "fields": [
                    {"key": "siigo_username",       "label": "Usuario Siigo",         "type": "text",     "required": True},
                    {"key": "siigo_access_key",     "label": "Access Key",            "type": "password", "required": True},
                    {"key": "document_id",          "label": "ID Tipo Documento FV",  "type": "text",     "required": True},
                    {"key": "seller_id",            "label": "ID Vendedor",           "type": "text",     "required": False},
                    {"key": "tax_id",               "label": "ID Impuesto IVA",       "type": "text",     "required": False},
                    {"key": "payment_id",           "label": "ID Forma de Pago",      "type": "text",     "required": True},
                    {"key": "product_code",         "label": "Código de Producto",    "type": "text",     "required": True},
                    {"key": "default_customer_nit", "label": "NIT Cliente Genérico",  "type": "text",     "required": True},
                    {"key": "iva_percentage",       "label": "% IVA",                 "type": "number",   "required": False},
                ],
                "docs": "https://siigonube.siigo.com/docs/"
            },
            {
                "id": "alegra",
                "name": "Alegra",
                "logo": "https://alegra.com/favicon.ico",
                "fields": [
                    {"key": "alegra_email",         "label": "Email Alegra",          "type": "email",    "required": True},
                    {"key": "alegra_token",         "label": "Token API",             "type": "password", "required": True},
                    {"key": "item_id_default",      "label": "ID Producto por defecto","type": "text",    "required": True},
                    {"key": "payment_type_id",      "label": "ID Forma de Pago",      "type": "text",     "required": True},
                    {"key": "warehouse_id",         "label": "ID Bodega",             "type": "text",     "required": False},
                    {"key": "iva_id",               "label": "ID Impuesto IVA",       "type": "text",     "required": False},
                    {"key": "default_customer_id",  "label": "ID Cliente Genérico",   "type": "text",     "required": True},
                    {"key": "currency",             "label": "Moneda",                "type": "select",   "required": False,
                     "options": ["COP", "USD", "EUR"]},
                ],
                "docs": "https://developer.alegra.com/docs"
            },
            {
                "id": "loggro",
                "name": "Loggro",
                "logo": "https://loggro.com/favicon.ico",
                "fields": [
                    {"key": "loggro_api_key",        "label": "API Key",               "type": "password", "required": True},
                    {"key": "loggro_company_id",     "label": "ID Empresa",            "type": "text",     "required": True},
                    {"key": "resolution_id",         "label": "ID Resolución DIAN",    "type": "text",     "required": True},
                    {"key": "payment_method_code",   "label": "Código Pago",           "type": "text",     "required": True},
                    {"key": "product_code_default",  "label": "Código Producto",       "type": "text",     "required": True},
                    {"key": "customer_nit_default",  "label": "NIT Cliente Genérico",  "type": "text",     "required": True},
                    {"key": "iva_percentage",        "label": "% IVA",                 "type": "number",   "required": False},
                ],
                "docs": "https://desarrolladores.loggro.com"
            }
        ]
    }


# ── ADMIN ENDPOINT (solo para superadmin) ────────────────────────────

@router.post("/admin/config")
async def admin_set_config(payload: AdminConfigPayload):
    if payload.admin_key != os.getenv("ADMIN_KEY"):
        raise HTTPException(status_code=403, detail="No autorizado")
    await save_billing_config(payload.restaurant_id, payload.config)
    return {"success": True}


@router.get("/admin/logs")
async def admin_logs(admin_key: str, restaurant_id: int, limit: int = 100):
    if admin_key != os.getenv("ADMIN_KEY"):
        raise HTTPException(status_code=403, detail="No autorizado")
    log = await get_billing_log(restaurant_id, limit)
    return {"log": log}