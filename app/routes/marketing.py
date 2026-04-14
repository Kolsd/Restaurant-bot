"""
Marketing routes — Clientes en Riesgo feature.

Endpoints:
  GET  /api/marketing/status
  GET  /api/customers/at-risk
  GET  /api/marketing/history
  PATCH /api/marketing/settings
  POST /api/marketing/send-reengagement

All routes require admin auth via get_current_restaurant.
No SQL here — all DB access through marketing_repo.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.repositories import marketing_repo
from app.routes.deps import get_current_restaurant
from app.services import state_store
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

# E.164 phone: optional leading +, then 10–15 digits
_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


# ── Pydantic models ───────────────────────────────────────────────────────────

class MarketingSettingsBody(BaseModel):
    enabled: bool


class ReengagementBody(BaseModel):
    customer_phone: str
    message: Optional[str] = None


# ── Internal WhatsApp send helper ─────────────────────────────────────────────

async def _send_whatsapp_marketing(
    phone: str,
    message: str,
    wa_access_token: str,
    wa_phone_id: str,
) -> tuple[bool, Optional[str]]:
    """Send a WhatsApp text message via Meta Cloud API.

    Returns (success: bool, meta_message_id: str | None).
    Uses restaurant-specific credentials (wa_access_token, wa_phone_id).
    Falls back to env vars if restaurant credentials are missing.
    """
    token    = wa_access_token or os.getenv("META_ACCESS_TOKEN", "")
    phone_id = wa_phone_id     or os.getenv("META_PHONE_NUMBER_ID", "")

    if not token or not phone_id:
        log.warning("marketing.whatsapp_not_configured", phone=phone)
        raise RuntimeError("WhatsApp credentials not configured for this restaurant")

    clean_phone = phone.lstrip("+").replace(" ", "")
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to":   clean_phone,
        "type": "text",
        "text": {"body": message},
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=body,
                headers={
                    "Authorization":  f"Bearer {token}",
                    "Content-Type":   "application/json",
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            meta_id = None
            try:
                meta_id = data["messages"][0]["id"]
            except (KeyError, IndexError, TypeError):
                pass
            return True, meta_id
        else:
            raise RuntimeError(f"Meta API returned {resp.status_code}: {resp.text[:200]}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"HTTP error sending WhatsApp: {exc}") from exc


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/marketing/status")
async def get_marketing_status(
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return plan, cap, sent_this_month, remaining, enabled, cost_estimate_month_usd."""
    restaurant_id = restaurant["id"]
    status = await marketing_repo.get_marketing_status(restaurant_id)
    # JSON boundary: Decimal → float
    status["cost_estimate_month_usd"] = float(status["cost_estimate_month_usd"])  # JSON boundary
    return status


@router.get("/api/customers/at-risk")
async def get_at_risk_customers(
    limit: int = Query(default=50, ge=1, le=200),
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return list of dormant frequent customers ordered by days_since DESC."""
    restaurant_id = restaurant["id"]
    customers = await marketing_repo.get_at_risk_customers(restaurant_id, limit=limit)
    return {"customers": customers, "count": len(customers)}


@router.get("/api/marketing/history")
async def get_marketing_history(
    limit: int = Query(default=50, ge=1, le=200),
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return recent marketing message log (newest first)."""
    restaurant_id = restaurant["id"]
    messages = await marketing_repo.get_recent_marketing_messages(restaurant_id, limit=limit)
    return {"messages": messages}


@router.patch("/api/marketing/settings")
async def patch_marketing_settings(
    body: MarketingSettingsBody,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Enable or disable marketing for the restaurant."""
    restaurant_id = restaurant["id"]
    updated = await marketing_repo.toggle_marketing_enabled(restaurant_id, body.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    log.info(
        "marketing.settings_updated",
        restaurant_id=restaurant_id,
        enabled=body.enabled,
    )
    return {"ok": True, "marketing_enabled": body.enabled}


@router.post("/api/marketing/send-reengagement")
async def send_reengagement(
    body: ReengagementBody,
    request: Request,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Send a re-engagement WhatsApp message to a single at-risk customer.

    Validates phone, checks cap, applies rate limit, sends, and logs.
    """
    restaurant_id = restaurant["id"]
    restaurant_name = restaurant.get("name", "tu restaurante")

    # 1. Validate phone (E.164)
    if not _PHONE_RE.match(body.customer_phone):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_phone", "message": "El teléfono debe ser E.164 (ej: +573001234567)"},
        )

    # 2. Validate message length and apply default template
    if body.message:
        if len(body.message) > 1024:
            raise HTTPException(
                status_code=400,
                detail={"error": "message_too_long", "message": "El mensaje no puede superar 1024 caracteres"},
            )
        final_message = body.message
    else:
        final_message = (
            f"Hola 👋 Te extrañamos en {restaurant_name}. "
            f"¿Volvemos a verte pronto? 🍽️"
        )

    # 3. Rate limit per restaurant (cross-worker via Redis)
    rate_ok = await state_store.rate_limit_check(
        f"marketing:{restaurant_id}", max_requests=10, window_seconds=60
    )
    if not rate_ok:
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "message": "Demasiados envíos. Espera un momento e intenta de nuevo."},
        )

    # 4. Check marketing cap + enabled status
    allowed, reason = await marketing_repo.can_send_marketing(restaurant_id)

    if not allowed:
        if reason == "disabled":
            await marketing_repo.log_marketing_message(
                restaurant_id=restaurant_id,
                customer_phone=body.customer_phone,
                message_type="reengagement",
                status="blocked_disabled",
                message_body=final_message,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "marketing_disabled",
                    "message": "El marketing está desactivado para este restaurante. Actívalo en Configuración.",
                },
            )

        if reason == "cap_reached":
            # Get cap info to include in error message
            status_info = await marketing_repo.get_marketing_status(restaurant_id)
            cap = status_info.get("cap", 0)
            await marketing_repo.log_marketing_message(
                restaurant_id=restaurant_id,
                customer_phone=body.customer_phone,
                message_type="reengagement",
                status="blocked_cap",
                message_body=final_message,
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "cap_reached",
                    "message": (
                        f"Alcanzaste el límite de {cap} mensajes este mes. "
                        "Actualiza tu plan para enviar más."
                    ),
                },
            )

        # restaurant_not_found or unknown
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Restaurante no encontrado"})

    # 5. Get restaurant WhatsApp credentials
    wa_access_token = restaurant.get("wa_access_token") or ""
    wa_phone_id     = restaurant.get("wa_phone_id") or ""

    # 6. Send via Meta Cloud API
    try:
        success, meta_message_id = await _send_whatsapp_marketing(
            phone=body.customer_phone,
            message=final_message,
            wa_access_token=wa_access_token,
            wa_phone_id=wa_phone_id,
        )
    except RuntimeError as exc:
        log.error(
            "marketing.send_failed",
            restaurant_id=restaurant_id,
            phone=body.customer_phone,
            error=str(exc),
        )
        await marketing_repo.log_marketing_message(
            restaurant_id=restaurant_id,
            customer_phone=body.customer_phone,
            message_type="reengagement",
            status="failed",
            message_body=final_message,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "send_failed",
                "message": "No se pudo enviar el mensaje. Verifica tu configuración de WhatsApp.",
            },
        )

    # 7. Log success
    await marketing_repo.log_marketing_message(
        restaurant_id=restaurant_id,
        customer_phone=body.customer_phone,
        message_type="reengagement",
        status="sent",
        message_body=final_message,
        meta_message_id=meta_message_id,
        cost_estimate_usd=marketing_repo.MARKETING_COST_USD_DEFAULT,
    )

    log.info(
        "marketing.reengagement_sent",
        restaurant_id=restaurant_id,
        phone=body.customer_phone,
        meta_message_id=meta_message_id,
    )

    # Get updated remaining count
    updated_status = await marketing_repo.get_marketing_status(restaurant_id)
    remaining = updated_status.get("remaining")  # None if enterprise

    return {
        "ok": True,
        "remaining": remaining,
        "meta_message_id": meta_message_id,
    }
