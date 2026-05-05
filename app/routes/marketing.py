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
from app.routes.deps import get_current_restaurant_scoped
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


class SendCampaignBody(BaseModel):
    """Bulk send to a vetted list of phones (e.g. selection from clientes-riesgo).

    Hard caps protect both the restaurant (against accidental mass blast) and
    the platform (against rate-limit / spam from Meta).  Per-call limits:
      - max 50 phones per request
      - message <= 1000 chars
    The endpoint also enforces the same `can_send_marketing` cap as the 1:1
    re-engagement path, with PARTIAL acceptance: it sends until the cap is hit,
    then aborts the rest with status='blocked_cap' so the admin sees what
    actually went out.
    """
    message: str
    customer_phones: list[str]


# Anti-mass-spam constants for /send-campaign.
_CAMPAIGN_MAX_PHONES = 50
_CAMPAIGN_MAX_MESSAGE_CHARS = 1000


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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return list of dormant frequent customers ordered by days_since DESC."""
    restaurant_id = restaurant["id"]
    customers = await marketing_repo.get_at_risk_customers(restaurant_id, limit=limit)
    return {"customers": customers, "count": len(customers)}


@router.get("/api/marketing/history")
async def get_marketing_history(
    limit: int = Query(default=50, ge=1, le=200),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return recent marketing message log (newest first)."""
    restaurant_id = restaurant["id"]
    messages = await marketing_repo.get_recent_marketing_messages(restaurant_id, limit=limit)
    return {"messages": messages}


@router.patch("/api/marketing/settings")
async def patch_marketing_settings(
    body: MarketingSettingsBody,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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


# ── Bulk send (campaigns) ─────────────────────────────────────────────────────


@router.post("/api/marketing/send-campaign")
async def send_campaign(
    body: SendCampaignBody,
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Bulk-send a marketing message to a curated list of customer phones.

    Pre-flight validation (fail fast, no partial sends if any of these trip):
      - phone count between 1 and 50
      - message length between 1 and 1000 chars
      - marketing enabled and cap not yet reached (one shared check upfront)
      - per-restaurant rate limit (Redis cross-worker)

    Per-phone result:
      - validates E.164 format; bad phones → status='invalid_phone'
      - sends via Meta Cloud API; success → log status='sent'
      - send failure → log status='failed' with the error
      - if cap is hit mid-batch → remaining phones logged as 'blocked_cap'

    Response body:
      {
        "ok": True,
        "sent":   <int>,
        "failed": <int>,
        "skipped": <int>,         # invalid_phone or blocked_cap
        "remaining": <int|None>,  # post-batch monthly remaining
        "errors": [{"phone": ..., "error": ...}, ...]
      }
    """
    restaurant_id = restaurant["id"]
    phones = body.customer_phones or []

    # 1. Hard caps on payload shape
    if not phones:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_phone_list", "message": "Debes enviar al menos un teléfono"},
        )
    if len(phones) > _CAMPAIGN_MAX_PHONES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_phones",
                "message": f"Máximo {_CAMPAIGN_MAX_PHONES} teléfonos por envío. Recibidos: {len(phones)}.",
            },
        )

    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_message", "message": "El mensaje no puede estar vacío"},
        )
    if len(msg) > _CAMPAIGN_MAX_MESSAGE_CHARS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "message_too_long",
                "message": f"El mensaje no puede superar {_CAMPAIGN_MAX_MESSAGE_CHARS} caracteres",
            },
        )

    # 2. Rate limit per restaurant (one campaign-send per minute is plenty —
    #    discourages running the loop in a tight UI re-click).
    rate_ok = await state_store.rate_limit_check(
        f"marketing_campaign:{restaurant_id}", max_requests=2, window_seconds=60
    )
    if not rate_ok:
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "message": "Esperá un momento antes de lanzar otra campaña."},
        )

    # 3. Pre-flight cap check (single round-trip — we re-check after each send
    #    against the running counter so partial-cap-hit aborts cleanly).
    allowed, reason = await marketing_repo.can_send_marketing(restaurant_id)
    if not allowed:
        if reason == "disabled":
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "marketing_disabled",
                    "message": "El marketing está desactivado para este restaurante.",
                },
            )
        if reason == "cap_reached":
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "cap_reached",
                    "message": "Ya alcanzaste el límite de mensajes este mes. Actualizá tu plan.",
                },
            )
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Restaurante no encontrado"})

    # 4. Resolve cap + cost upfront so we can stop mid-batch deterministically
    status_info = await marketing_repo.get_marketing_status(restaurant_id)
    cap = status_info.get("cap")             # None for enterprise
    sent_so_far = int(status_info.get("sent_this_month") or 0)
    remaining_budget = None if cap is None else max(0, int(cap) - sent_so_far)

    wa_access_token = restaurant.get("wa_access_token") or ""
    wa_phone_id     = restaurant.get("wa_phone_id") or ""

    sent = 0
    failed = 0
    skipped = 0
    errors: list[dict] = []

    for phone in phones:
        # 4a. E.164 sanity
        if not isinstance(phone, str) or not _PHONE_RE.match(phone.strip()):
            skipped += 1
            errors.append({"phone": str(phone)[:30], "error": "invalid_phone"})
            try:
                await marketing_repo.log_marketing_message(
                    restaurant_id=restaurant_id,
                    customer_phone=str(phone)[:30],
                    message_type="campaign",
                    status="invalid_phone",
                    message_body=msg,
                    error="invalid_phone_format",
                )
            except Exception:
                log.exception("marketing.campaign_log_failed", phone=str(phone)[:30])
            continue

        # 4b. Cap exhausted mid-batch → mark remaining as blocked_cap and break
        if remaining_budget is not None and remaining_budget <= 0:
            skipped += 1
            errors.append({"phone": phone, "error": "blocked_cap"})
            try:
                await marketing_repo.log_marketing_message(
                    restaurant_id=restaurant_id,
                    customer_phone=phone,
                    message_type="campaign",
                    status="blocked_cap",
                    message_body=msg,
                )
            except Exception:
                log.exception("marketing.campaign_log_failed", phone=phone)
            continue

        # 4c. Send
        try:
            success, meta_message_id = await _send_whatsapp_marketing(
                phone=phone,
                message=msg,
                wa_access_token=wa_access_token,
                wa_phone_id=wa_phone_id,
            )
        except RuntimeError as exc:
            failed += 1
            err = str(exc)[:200]
            errors.append({"phone": phone, "error": err})
            try:
                await marketing_repo.log_marketing_message(
                    restaurant_id=restaurant_id,
                    customer_phone=phone,
                    message_type="campaign",
                    status="failed",
                    message_body=msg,
                    error=err,
                )
            except Exception:
                log.exception("marketing.campaign_log_failed", phone=phone)
            continue

        if success:
            sent += 1
            if remaining_budget is not None:
                remaining_budget -= 1
            try:
                await marketing_repo.log_marketing_message(
                    restaurant_id=restaurant_id,
                    customer_phone=phone,
                    message_type="campaign",
                    status="sent",
                    message_body=msg,
                    meta_message_id=meta_message_id,
                    cost_estimate_usd=marketing_repo.MARKETING_COST_USD_DEFAULT,
                )
            except Exception:
                log.exception("marketing.campaign_log_failed", phone=phone)
        else:
            failed += 1
            errors.append({"phone": phone, "error": "send_returned_false"})

    log.info(
        "marketing.campaign_sent",
        restaurant_id=restaurant_id,
        total=len(phones),
        sent=sent,
        failed=failed,
        skipped=skipped,
    )

    return {
        "ok": True,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "remaining": remaining_budget,
        "errors": errors[:20],   # cap response payload
    }
