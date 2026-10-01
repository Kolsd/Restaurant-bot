"""
app/routes/diner_memory.py
==========================
"Recuérdame" for the diner web chat (migration 0104, app/services/diner_memory.py).

    POST /api/diner/memory/consent — the diner says yes: this browser's
                                      secret becomes a profile and the current
                                      session (and the order just sent) counts.
    POST /api/diner/memory/forget  — "olvidarme": the profile is deleted.
    POST /api/diner/memory/repeat  — the last visit's dishes back into the cart.

Public and unauthenticated like the rest of /api/diner: the session token
names the org, the memory key proves the browser. Rate-limited per token
through state_store (Rule 7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.repositories import customer_profiles_repo, diner_sessions_repo
from app.routes.diner import _cart_blocks_response, _currency_for_org, _resolve_session_or_404
from app.services import diner_memory, orders, state_store
from app.services.logging import get_logger
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/diner/memory", tags=["diner"])


class MemoryKeyRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    memory_key: str = Field(..., min_length=1, max_length=200)


class MemoryRepeatRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)


async def _throttle(name: str, token: str) -> None:
    if not await state_store.rate_limit_check(f"diner_memory_{name}:{token}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Espera un momento e intenta de nuevo.")


def _key_or_422(raw: str) -> str:
    key = diner_memory.device_key(raw)
    if not key:
        raise HTTPException(status_code=422, detail="No pudimos recordar este celular.")
    return key


@router.post("/consent")
async def memory_consent(body: MemoryKeyRequest):
    token = body.token.strip()
    await _throttle("consent", token)
    key = _key_or_422(body.memory_key)
    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])

    with tenant_scope(org_id):
        profile = await customer_profiles_repo.remember_device(org_id, key)
        await diner_sessions_repo.link_profile(token, org_id, profile["id"])

    log.info("diner_memory.consented", org_id=org_id, profile_id=profile["id"])
    return {
        "remembered": True,
        "message": "Listo, la próxima vez te saludamos con lo que te gusta.",
    }


@router.post("/forget")
async def memory_forget(body: MemoryKeyRequest):
    token = body.token.strip()
    await _throttle("forget", token)
    key = _key_or_422(body.memory_key)
    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])

    with tenant_scope(org_id):
        # The key, not the session, says whose memory this is: a session
        # token alone (e.g. shared at the table) can't erase someone else.
        profile = await customer_profiles_repo.get_remembered_device(org_id, key)
        deleted = await customer_profiles_repo.delete_profile(org_id, profile["id"]) if profile else False

    log.info("diner_memory.forgotten", org_id=org_id, deleted=deleted)
    return {
        "remembered": False,
        "message": "Listo, olvidamos tus pedidos en este celular.",
    }


@router.post("/repeat")
async def memory_repeat(body: MemoryRepeatRequest):
    token = body.token.strip()
    await _throttle("repeat", token)
    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    profile_id = session.get("customer_profile_id")
    if not profile_id:
        raise HTTPException(status_code=404, detail="No tenemos pedidos anteriores guardados en este celular.")

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        result = await diner_memory.repeat_last_order(
            token, org_id, session.get("location_id"), int(profile_id),
        )
        currency = await _currency_for_org(org_id)
        cart = result["cart"]
        if cart is None:
            cart = await orders.get_cart_with_line_ids(token, org_id)

    if not result["added"]:
        message = "Hoy no tenemos disponible nada de tu último pedido aquí. Mira la carta, seguro algo te provoca."
    elif result["skipped"]:
        message = (
            "Agregamos tu último pedido. Hoy no tenemos "
            + ", ".join(result["skipped"]) + "."
        )
    else:
        message = "Agregamos tu último pedido. Revísalo y envíalo cuando quieras."
    response = _cart_blocks_response(cart, currency, message)
    response["added"] = result["added"]
    response["skipped"] = result["skipped"]
    return response
