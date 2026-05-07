"""
Mesio — Rutas de Billing / Facturación
"""

import json
import os
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services import database as db
from app.routes.deps import get_current_user, get_current_restaurant_scoped
from app.services.billing import (
    get_billing_config,
    save_billing_config,
    get_billing_log,
    emit_invoice,
    get_adapter,
)
from app.services.tenant_context import tenant_scope

router = APIRouter(prefix="/api/billing", tags=["billing"])


# ── AUTH HELPER ───────────────────────────────────────────────────────

async def _get_restaurant_id(user: dict) -> int:
    # 1. Si es un empleado (Staff con PIN), ya tiene el ID directo
    if "restaurant_id" in user:
        return user["restaurant_id"]

    # 2. Si es un Admin/Dueño, usamos su branch_id
    if user.get("branch_id"):
        return user["branch_id"]

    # Name-based fallback removed: IDOR risk — trust only the JWT claims.
    raise HTTPException(status_code=401, detail="Token no contiene restaurante asignado")

# ── MODELOS ──────────────────────────────────────────────────────────

class BillingConfigPayload(BaseModel):
    provider:    str  # "siigo" | "alegra" | "loggro" | "mesio_native"
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
    # Mesio Native (DIAN)
    restaurant_nit:          Optional[str]   = None  # NIT sin dígito verificación, ej: "900123456"
    restaurant_legal_name:   Optional[str]   = None  # Razón social
    restaurant_city_code:    Optional[str]   = None  # Código DANE municipio, ej: "11001"
    restaurant_city_name:    Optional[str]   = None  # ej: "Bogotá"
    restaurant_address_dian: Optional[str]   = None  # Dirección fiscal
    tax_regime:              Optional[str]   = None  # "iva" | "ico" (Impuesto al Consumo)
    nit_id_type:             Optional[str]   = None  # "31"=NIT (default) | "13"=CC
    software_id:             Optional[str]   = None  # ID software habilitado en DIAN
    software_pin:            Optional[str]   = None  # PIN del software DIAN
    dian_environment:        Optional[str]   = None  # "test" | "production"

class EmitInvoicePayload(BaseModel):
    order_id:  str
    customer:  Optional[dict] = None  # {nit, name, email, alegra_id}

# ── ENDPOINTS ────────────────────────────────────────────────────────
# NOTE: Admin billing endpoints (/api/billing/admin/*) have been moved to
# app/routes/internal/billing_admin.py under /api/internal/billing/*.

@router.get("/config")
async def get_config(request: Request):
    user          = await get_current_user(request)
    restaurant_id = await _get_restaurant_id(user)
    with tenant_scope(restaurant_id):
        config = await get_billing_config(restaurant_id)
    if not config:
        return {"configured": False}
    # Ocultar secretos en la respuesta
    safe = {k: ("***" if "key" in k.lower() or "token" in k.lower() or "password" in k.lower() or "secret" in k.lower() else v)
            for k, v in config.items()}
    return {"configured": True, "config": safe}


@router.post("/config")
async def set_config(request: Request, payload: BillingConfigPayload):
    user          = await get_current_user(request)
    restaurant_id = await _get_restaurant_id(user)

    allowed = {"siigo", "alegra", "loggro", "mesio_native"}
    if payload.provider.lower() not in allowed:
        raise HTTPException(status_code=400, detail=f"Proveedor debe ser uno de: {allowed}")

    config = payload.model_dump(exclude_none=True)
    with tenant_scope(restaurant_id):
        await save_billing_config(restaurant_id, config)
    return {"success": True, "provider": payload.provider}


@router.post("/emit")
async def emit(request: Request, payload: EmitInvoicePayload):
    """Emite manualmente una factura para un pedido específico."""
    from app.services.database import UsageLimitExceeded  # noqa: PLC0415

    user          = await get_current_user(request)
    restaurant_id = await _get_restaurant_id(user)
    with tenant_scope(restaurant_id):
        try:
            result = await emit_invoice(payload.order_id, restaurant_id, payload.customer)
        except UsageLimitExceeded as exc:
            # Plan-limit guard fired; surface as HTTP 429 so the UI can show
            # "upgrade your plan" instead of a generic technical error.
            raise HTTPException(status_code=429, detail=str(exc))
    if not result["success"]:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.get("/log")
async def billing_log(request: Request, limit: int = 50):
    user          = await get_current_user(request)
    restaurant_id = await _get_restaurant_id(user)
    with tenant_scope(restaurant_id):
        log = await get_billing_log(restaurant_id, limit)
    return {"log": log}


@router.post("/test-connection")
async def test_connection(request: Request):
    """Prueba las credenciales sin emitir factura real."""
    user          = await get_current_user(request)
    restaurant_id = await _get_restaurant_id(user)
    with tenant_scope(restaurant_id):
        config = await get_billing_config(restaurant_id)

    if not config:
        raise HTTPException(status_code=400, detail="Billing no configurado")

    provider = config.get("provider", "").lower()
    try:
        adapter = get_adapter(provider)
        result  = await adapter.test_connection(config)
        return {"success": True, "provider": provider, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ── PLAN DOWNGRADE ROUTES ─────────────────────────────────────────────────────


class DowngradeRequestPayload(BaseModel):
    new_plan_code: str
    kept_location_id: int


# Plan sort order (smaller index = smaller plan) — mirrors plan_limits_repo
_PLAN_ORDER = ["pulso", "restaurante", "pro", "cadena"]
_PLAN_LOCATIONS = {
    "pulso": 1,
    "restaurante": 3,
    "pro": 10,
    "cadena": None,  # unlimited
}
_PLAN_PRICES = {
    "pulso": 149_000,
    "restaurante": 299_000,
    "pro": 549_000,
    "cadena": 899_000,
}


@router.get("/plan-options")
async def plan_options(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List all plans with sucursales_max and monthly_price_cop for the UI dropdown."""
    from app.repositories.plan_limits_repo import db_list_plans  # noqa: PLC0415
    plans = await db_list_plans()
    result = []
    for p in plans:
        code = p["plan_code"]
        result.append({
            "plan_code": code,
            "display_name": p.get("display_name", code.capitalize()),
            "sucursales_max": p.get("locations_included"),  # None = unlimited
            "monthly_price_cop": p.get("monthly_price_cop"),
        })
    return {"plans": result}


@router.get("/plan-status")
async def plan_status(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return current plan, pending downgrade state, and list of current sucursales."""
    from app.repositories.plan_limits_repo import db_get_org_subscription, db_get_pending_downgrade  # noqa: PLC0415
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415

    org_id = restaurant["id"]
    sub = await db_get_org_subscription(org_id)
    pending = await db_get_pending_downgrade(org_id)

    # Fetch current sucursales (locations) under bypass (locations table has no RLS)
    with bypass_tenant_scope("billing.plan_status.list_locations"):
        locations = await db.db_get_org_locations(org_id, active_only=False)
    sucursales = [
        {"id": loc["id"], "name": loc.get("name", f"Sede {loc['id']}")}
        for loc in locations
    ]

    return {
        "current_plan": sub.get("plan_code"),
        "current_plan_display": sub.get("plan_display_name"),
        "current_sucursales": sucursales,
        "pending_plan": pending.get("pending_plan_code") if pending else None,
        "effective_at": pending.get("pending_plan_effective_at") if pending else None,
        "kept_location_id": pending.get("pending_kept_location_id") if pending else None,
    }


@router.post("/request-downgrade")
async def request_downgrade(
    payload: DowngradeRequestPayload,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Schedule a plan downgrade (effective in 7 days).

    Validates: new_plan_code must be smaller than current, kept_location_id
    must belong to this org.
    """
    from app.repositories.plan_limits_repo import db_request_downgrade  # noqa: PLC0415

    org_id = restaurant["id"]
    try:
        result = await db_request_downgrade(
            org_id=org_id,
            new_plan_code=payload.new_plan_code,
            kept_location_id=payload.kept_location_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "pending": result}


@router.post("/cancel-downgrade")
async def cancel_downgrade(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Cancel a scheduled plan downgrade."""
    from app.repositories.plan_limits_repo import db_cancel_downgrade  # noqa: PLC0415

    org_id = restaurant["id"]
    existed = await db_cancel_downgrade(org_id)
    return {"success": True, "cancelled": existed}


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
            },
            {
                "id": "mesio_native",
                "name": "Mesio Native (DIAN Colombia)",
                "logo": "/static/mesio-icon.png",
                "fields": [
                    {"key": "restaurant_nit",          "label": "NIT del restaurante",          "type": "text",     "required": True},
                    {"key": "restaurant_legal_name",   "label": "Razón social",                 "type": "text",     "required": True},
                    {"key": "restaurant_city_code",    "label": "Código DANE municipio",        "type": "text",     "required": True},
                    {"key": "restaurant_city_name",    "label": "Ciudad",                       "type": "text",     "required": True},
                    {"key": "restaurant_address_dian", "label": "Dirección fiscal",             "type": "text",     "required": True},
                    {"key": "tax_regime",              "label": "Régimen tributario",           "type": "select",   "required": True,
                     "options": ["iva", "ico"],
                     "hint": "iva=IVA 19% | ico=Impto. al Consumo 8%"},
                    {"key": "nit_id_type",             "label": "Tipo ID emisor",               "type": "select",   "required": False,
                     "options": ["31", "13"], "hint": "31=NIT | 13=Cédula"},
                    {"key": "software_id",             "label": "ID Software DIAN",             "type": "text",     "required": True},
                    {"key": "software_pin",            "label": "PIN Software DIAN",            "type": "password", "required": True},
                    {"key": "dian_environment",        "label": "Ambiente DIAN",                "type": "select",   "required": True,
                     "options": ["test", "production"]},
                    {"key": "currency",                "label": "Moneda",                       "type": "select",   "required": False,
                     "options": ["COP"]},
                ],
                "docs": "https://www.dian.gov.co/impuestos/factura-electronica"
            }
        ]
    }

