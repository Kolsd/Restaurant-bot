import os
import time
import base64
import hmac
import hashlib
import httpx
import traceback
from collections import defaultdict
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from anthropic import AsyncAnthropic
from app.services.agent import chat, reset_conversation
from app.services import database as db
from app.repositories import inbox_repo, conversations_repo
from app.services.logging import get_logger
from app.services.state_store import rate_limit_check
from app.routes.deps import get_current_user
from app.services.tenant_db import PoolAcquireTimeout

log = get_logger(__name__)

router = APIRouter()

# ── RATE LIMITING BACKED BY POSTGRES (Workers Safe) ──────────────────
RATE_LIMIT_MESSAGES = 20   # max mensajes por ventana
RATE_LIMIT_WINDOW   = 60   # segundos

async def _is_rate_limited(phone: str) -> bool:
    return await conversations_repo.db_check_rate_limit(
        phone, RATE_LIMIT_WINDOW, RATE_LIMIT_MESSAGES
    )

# ── META SIGNATURE VERIFICATION (V-02) ──────────────────────────────
def _verify_meta_signature(body: bytes, signature_header: str) -> bool:
    """Verifica X-Hub-Signature-256 de Meta para autenticar el webhook.

    When the env var DISABLE_META_SIGNATURE_VERIFY=1 is set (E2E test mode only),
    signature verification is skipped and the function returns True unconditionally.
    NEVER set this in production.
    """
    if os.getenv("DISABLE_META_SIGNATURE_VERIFY", "").strip() in ("1", "true", "yes"):
        return True
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_secret:
        log.error("meta.signature.no_secret_configured")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        log.warning("meta.signature.missing_or_invalid_header")
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _normalize_number(number: str) -> str:
    if not number: return ""
    return number.replace(" ", "").replace("+", "")


META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")


class ChatRequest(BaseModel):
    phone: str
    message: str
    bot_number: str = ""


class ResetRequest(BaseModel):
    phone: str


@router.post("/chat")
async def chat_endpoint(request: ChatRequest):
    result = await chat(request.phone, request.message, request.bot_number)
    if result is None: return {"success": True, "response": ""}
    return {"success": True, "response": result["message"]}


@router.post("/reset")
async def reset_chat(request: ResetRequest):
    await reset_conversation(request.phone)
    return {"success": True, "message": f"Conversacion de {request.phone} reiniciada"}

@router.get("/media/{media_id}")
async def get_whatsapp_media(
    media_id: str,
    bot: str = "",
    user: dict = Depends(get_current_user),
):
    """
    Descarga la imagen encriptada desde Meta y la muestra en el navegador del Cajero.

    Requiere autenticación (Bearer token de admin/staff del tenant).
    El parámetro `bot` es obligatorio y debe corresponder al tenant del usuario autenticado.
    Rate limit: 60 req/min por usuario.
    """
    # ── Ownership: el bot_number debe pertenecer al tenant del usuario autenticado ──
    if not bot:
        raise HTTPException(status_code=400, detail="Parámetro bot requerido")

    tenant_restaurant_id = user.get("restaurant_id") or user.get("branch_id")
    if not tenant_restaurant_id:
        raise HTTPException(status_code=403, detail="No autorizado")

    # Verificar que el bot_number pertenece al tenant (o a una sucursal suya)
    from app.services.tenant_context import bypass_tenant_scope
    with bypass_tenant_scope("media_proxy_ownership_check"):
        tenant_rest = await db.db_get_restaurant_by_phone(bot)
    if not tenant_rest:
        raise HTTPException(status_code=403, detail="No autorizado")
    rest_id = tenant_rest["id"]
    parent_id = tenant_rest.get("parent_restaurant_id")
    if rest_id != tenant_restaurant_id and parent_id != tenant_restaurant_id:
        raise HTTPException(status_code=403, detail="No autorizado")

    # ── Rate limit: 60 req/min por usuario ──────────────────────────────────────
    user_key = user.get("username") or str(tenant_restaurant_id)
    allowed = await rate_limit_check(f"media:{user_key}", max_requests=60, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    # ── Descargar desde Meta ─────────────────────────────────────────────────────
    token = tenant_rest.get("wa_access_token") or os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Token de acceso no configurado")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        headers = {"Authorization": f"Bearer {token}"}
        res = await client.get(f"https://graph.facebook.com/{META_API_VERSION}/{media_id}", headers=headers)

        if res.status_code == 200:
            data = res.json()
            media_url = data.get("url")
            if media_url:
                media_res = await client.get(media_url, headers=headers)
                if media_res.status_code == 200:
                    return Response(content=media_res.content, media_type=data.get("mime_type", "image/jpeg"))

    return Response(content="Imagen no encontrada o expirada", status_code=404)

@router.get("/webhook/meta")
async def verify_meta_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    verify_token = os.getenv("META_VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if not verify_token:
        return Response(content="Verify token not configured", status_code=500)
    if mode == "subscribe" and token == verify_token:
        return Response(content=challenge)
    return Response(content="Error de verificacion", status_code=403)


# ── BACKGROUND TASK (V-03: webhook async) ────────────────────────────
async def _process_message(
    user_phone: str,
    user_text: str,
    bot_number: str,
    phone_id: str,
    access_token: str,
    location_id: int | None = None,
):
    """Procesamiento real de la IA — corre en background, desacoplado del ACK.

    location_id: resolved by the inbox worker before dispatch.  May be None for
    exploratory chat; the agent resolves it lazily when needed (e.g. on delivery/
    pickup tool calls).  Passed through to agent.chat() so that it can persist
    the Location on the conversation row and route orders correctly.
    """
    try:
        result = await chat(
            user_phone,
            user_text,
            bot_number,
            meta_phone_id=phone_id,
            location_id=location_id,
        )
        log.info("chat.ai_result", phone=user_phone, has_message=bool(result and result.get("message")))

        if result and result.get("message"):
            url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages"
            headers = {"Authorization": f"Bearer {access_token}"}
            async with httpx.AsyncClient(timeout=10) as client:
                if result.get("interactive"):
                    # Send as interactive message with buttons
                    wa_payload = {
                        "messaging_product": "whatsapp",
                        "to": user_phone,
                        "type": "interactive",
                        "interactive": result["interactive"]
                    }
                else:
                    wa_payload = {
                        "messaging_product": "whatsapp",
                        "to": user_phone,
                        "type": "text",
                        "text": {"body": result["message"]}
                    }
                res = await client.post(url, headers=headers, json=wa_payload)
                log.info("chat.meta_send", phone=user_phone, status=res.status_code)
                if res.status_code != 200:
                    log.error("chat.meta_send_failed", phone=user_phone, status=res.status_code, body=res.text[:200])
    except Exception:
        log.exception("chat.process_message_failed", phone=user_phone)


async def _is_image_safe(image_id: str, access_token: str) -> bool:
    """Descarga la imagen desde Meta y consulta a Claude Haiku si contiene contenido inapropiado.
    Retorna False si el modelo responde YES (inapropiado), True en cualquier otro caso.
    En caso de excepción, retorna True (fail open — no bloquear por errores)."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            headers = {"Authorization": f"Bearer {access_token}"}
            meta_res = await client.get(
                f"https://graph.facebook.com/{META_API_VERSION}/{image_id}",
                headers=headers,
            )
            if meta_res.status_code != 200:
                return True
            data = meta_res.json()
            media_url = data.get("url")
            if not media_url:
                return True
            img_res = await client.get(media_url, headers=headers)
            if img_res.status_code != 200:
                return True
            image_bytes = img_res.content
            mime_type = data.get("mime_type", "image/jpeg")

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        haiku_client = AsyncAnthropic()
        response = await haiku_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Does this image contain inappropriate content "
                                "(NSFW content, violence, nudity, threats, or hateful imagery)? "
                                "Reply with only YES or NO."
                            ),
                        },
                    ],
                }
            ],
        )
        answer = response.content[0].text.strip().upper()
        # Si Claude se niega a clasificar (refusal por contenido extremo), tratar como unsafe
        if not answer.startswith("YES") and not answer.startswith("NO"):
            return False
        return not answer.startswith("YES")
    except Exception:
        log.warning("image_moderation.error", image_id=image_id, exc_info=True)
        return True


async def _send_wa_text(user_phone: str, text: str, phone_id: str, access_token: str):
    """Envía un mensaje de texto simple a WhatsApp sin pasar por la IA."""
    try:
        url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {access_token}"}
        payload = {
            "messaging_product": "whatsapp",
            "to": user_phone,
            "type": "text",
            "text": {"body": text},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(url, headers=headers, json=payload)
            if res.status_code != 200:
                log.error("chat.send_wa_text_failed", phone=user_phone, status=res.status_code, body=res.text[:200])
    except Exception:
        log.exception("chat.send_wa_text_error", phone=user_phone)


@router.post("/webhook/meta")
async def meta_webhook(request: Request, background_tasks: BackgroundTasks):
    import json as _json
    from app.services.tenant_context import bypass_tenant_scope  # noqa: PLC0415

    if not await rate_limit_check("webhook_global", max_requests=200, window_seconds=1):
        log.warning("webhook.global_rate_limit")
        return JSONResponse(content={"status": "ok"})

    # 1. Leer body ANTES de parsear JSON (necesitamos bytes para la firma)
    raw_body = await request.body()

    # 2. Verificar firma Meta — return 200 to prevent Meta retry storms on bad signatures
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_meta_signature(raw_body, signature):
        log.warning("meta.signature.verification_failed", signature_prefix=signature[:20] if signature else "")
        return JSONResponse(content={"status": "ok"})

    try:
        data = _json.loads(raw_body)
    except Exception:
        return JSONResponse(content={"status": "ok"})

    any_enqueue_failed = False
    all_failures_are_db_down = True  # flipped to False if any non-DB failure occurs
    # The webhook enqueue path is cross-tenant: the inbox worker resolves the
    # tenant at dispatch time.  bypass_tenant_scope allows migrated repos (e.g.
    # conversations_repo.db_is_duplicate_wam) to execute without a pinned tenant.
    with bypass_tenant_scope("webhook_enqueue_cross_tenant"):
        try:
            entries = data.get("entry", [])
            if not entries:
                return JSONResponse(content={"status": "ok"})

            for entry in entries:
                changes_list = entry.get("changes", [])
                if not changes_list:
                    continue
                changes = changes_list[0]
                value   = changes.get("value", {})

                if "messages" not in value:
                    continue

                messages_list = value.get("messages", [])
                if not messages_list:
                    continue
                message = messages_list[0]
                if not message:
                    continue

                # 3. Deduplicación por WAM_ID — descarta reintentos de Meta
                wam_id = message.get("id", "")
                if wam_id and await db.db_is_duplicate_wam(wam_id):
                    log.info("chat.wam_duplicate_ignored", wam_id=wam_id)
                    continue

                user_phone    = message.get("from", "")
                msg_type      = message.get("type", "text")
                raw_bot_number = value.get("metadata", {}).get("display_phone_number", "")
                bot_number    = _normalize_number(raw_bot_number)
                phone_id      = value.get("metadata", {}).get("phone_number_id", "")

                # 4. Credenciales dinámicas desde PostgreSQL
                restaurant = await db.db_get_restaurant_by_phone(bot_number)
                if restaurant and restaurant.get("wa_access_token"):
                    access_token = restaurant["wa_access_token"]
                    phone_id = restaurant.get("wa_phone_id") or phone_id
                else:
                    access_token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")

                # Auto-persist phone_id so delivery notifications can use it later.
                # wa_phone_id comes from Meta webhook metadata — always authoritative.
                if restaurant and phone_id and not restaurant.get("wa_phone_id"):
                    try:
                        await conversations_repo.db_update_restaurant_phone_id(restaurant["id"], phone_id)
                    except Exception:
                        log.exception("chat.update_phone_id_failed", restaurant_id=restaurant["id"], phone_id=phone_id)

                # 5. Ruta CRM — procesamiento inline (no requiere IA)
                crm_phone_id = os.getenv("CRM_PHONE_NUMBER_ID")
                if crm_phone_id and phone_id == crm_phone_id:
                    from app.routes.internal.crm import register_inbound_from_prospect
                    if msg_type == "location":
                        loc = message.get("location", {})
                        user_text = f"📍 Ubicación compartida: lat:{loc.get('latitude')} lon:{loc.get('longitude')}"
                    else:
                        user_text = message.get("text", {}).get("body", "")
                    if user_text and user_phone:
                        log.info("chat.crm_inbound", phone=user_phone, phone_id=phone_id)
                        await register_inbound_from_prospect(user_phone, user_text, wam_id)
                        log.info("chat.crm_message_saved", phone=user_phone)
                    continue

                # 6. Rate limiting
                is_limited = await _is_rate_limited(user_phone)
                if user_phone and is_limited:
                    log.warning("chat.rate_limited", phone=user_phone, bot_number=bot_number)
                    try:
                        await _send_wa_text(user_phone, "Un momento por favor, estoy procesando tu mensaje anterior.", phone_id, access_token)
                    except Exception:
                        log.exception("chat.send_ratelimit_msg_failed", phone=user_phone)
                    continue

                # 7. Extraer texto del mensaje
                if msg_type == "location":
                    loc = message.get("location", {})
                    lat, lon = loc.get("latitude"), loc.get("longitude")

                    if lat is not None and lon is not None:
                        # GPS coords are saved to cart by the inbox worker (within proper
                        # tenant_scope). Saving here is impossible — bypass_tenant_scope
                        # takes precedence over nested tenant_scope in tenant_context.py.
                        maps_url = f"https://maps.google.com/?q={lat},{lon}"
                        user_text = f"Mi ubicación es: {maps_url} (lat:{lat}, lon:{lon})."
                    else:
                        log.warning("chat.gps_missing_coordinates", phone=user_phone)
                        user_text = ""
                elif msg_type == "interactive":
                    button_reply = message.get("interactive", {}).get("button_reply", {})
                    # Capture the raw button id so _extract_location_from_payload
                    # can detect "loc:<id>" QR button payloads in the inbox worker.
                    _interactive_button_id = button_reply.get("id", "")
                    user_text = _interactive_button_id or button_reply.get("title", "")
                elif msg_type == "image":
                    image_id = message.get("image", {}).get("id", "")
                    media_url = f"/api/media/{image_id}?bot={bot_number}"

                    # Atajo no-LLM: si hay una propuesta awaiting_proof para este teléfono,
                    # adjuntar el comprobante directamente sin pasar por el modelo.
                    try:
                        restaurant_data = await db.db_get_restaurant_by_bot_number(bot_number)
                        if restaurant_data:
                            proposal = await db.db_get_open_proposal_for_phone(
                                restaurant_data["id"], user_phone
                            )
                            if proposal and proposal.get("proposal_status") == "awaiting_proof":
                                # Moderación de contenido: rechazar imágenes inapropiadas
                                is_safe = await _is_image_safe(image_id, access_token)
                                if not is_safe:
                                    rejection_msg = (
                                        "⚠️ Tu imagen no pudo ser procesada. "
                                        "Por favor envía una captura clara de tu comprobante de pago."
                                    )
                                    await _send_wa_text(user_phone, rejection_msg, phone_id, access_token)
                                    continue
                                await db.db_attach_proof(
                                    proposal["base_order_id"], user_phone, media_url
                                )
                                # Limpiar checkout state
                                from app.services import state_store as _ss
                                await _ss.checkout_delete(user_phone, bot_number)
                                # Enviar confirmación vía background task
                                confirm_msg = "✅ Comprobante recibido, caja lo está validando. ¡Gracias! 🙏"
                                background_tasks.add_task(
                                    _send_wa_text, user_phone, confirm_msg, phone_id, access_token
                                )
                                continue
                    except Exception as e:
                        log.error("chat.proof_shortcut_failed", phone=user_phone, error=str(e))

                    user_text = f"📸 [IMAGEN RECIBIDA] Link del comprobante: {media_url}"
                elif msg_type == "audio":
                    # ── Audio / voice note handling ──────────────────────────────
                    # Transcription is intentionally deferred to the inbox worker
                    # so the webhook returns fast to Meta (Rule 6).
                    audio_id = (message.get("audio") or {}).get("id")
                    if not audio_id:
                        log.warning("audio.no_id", wam_id=wam_id, phone=user_phone)
                        continue
                    if not user_phone:
                        continue
                    log.info("chat.audio_inbound", phone=user_phone, bot_number=bot_number, wam_id=wam_id, audio_id=audio_id)
                    try:
                        pool = await db.get_pool()
                        external_id = wam_id or None
                        if not external_id:
                            dedup_content = f"{user_phone}:audio:{audio_id}:{bot_number}:{int(time.time() // 10)}"
                            external_id = f"synth_{hashlib.sha256(dedup_content.encode()).hexdigest()[:16]}"
                        enqueue_payload = {
                            "user_phone":          user_phone,
                            "user_text":           "",
                            "audio_id":            audio_id,
                            "needs_transcription": True,
                            "bot_number":          bot_number,
                            "phone_id":            phone_id,
                        }
                        inserted = await inbox_repo.enqueue(
                            pool,
                            provider="meta_whatsapp",
                            external_id=external_id,
                            payload=enqueue_payload,
                        )
                        if not inserted:
                            log.info("inbox_dedup_skipped", wam_id=wam_id, user_phone=user_phone)
                    except (PoolAcquireTimeout, ConnectionError) as _db_exc:
                        log.error(
                            "chat.enqueue_failed_db_down",
                            wam_id=wam_id,
                            user_phone=user_phone,
                            exc=str(_db_exc),
                        )
                        any_enqueue_failed = True
                        # DB-connectivity failure — classified as db_down
                    except Exception:
                        log.exception("chat.enqueue_failed", wam_id=wam_id, user_phone=user_phone)
                        any_enqueue_failed = True
                        all_failures_are_db_down = False  # non-DB failure
                    continue  # audio path is fully handled above; skip text guard
                else:
                    user_text = message.get("text", {}).get("body", "")

                if not user_text or not user_phone:
                    continue

                log.info("chat.inbound", phone=user_phone, bot_number=bot_number, wam_id=wam_id, text_preview=user_text[:200])

                # 8. Persist to webhook_inbox — durable processing survives worker restarts.
                #    The inbox worker (inbox_worker.py) will call _process_message asynchronously.
                try:
                    pool = await db.get_pool()
                    # Ensure dedup coverage even when Meta sends no WAM ID.
                    # Synthetic ID hashes content in 10s windows so retries within
                    # the same window hit the unique index and are skipped.
                    external_id = wam_id or None
                    if not external_id:
                        dedup_content = f"{user_phone}:{user_text}:{bot_number}:{int(time.time() // 10)}"
                        external_id = f"synth_{hashlib.sha256(dedup_content.encode()).hexdigest()[:16]}"
                    enqueue_payload = {
                        "user_phone": user_phone,
                        "user_text":  user_text,
                        "bot_number": bot_number,
                        "phone_id":   phone_id,
                    }
                    # Carry GPS coords so inbox_worker can persist to cart within tenant_scope
                    if msg_type == "location" and lat is not None and lon is not None:
                        enqueue_payload["gps_lat"] = lat
                        enqueue_payload["gps_lon"] = lon
                    # Carry raw button_id so _extract_location_from_payload can detect
                    # QR deep-links embedded as "loc:<id>" interactive button payloads.
                    if msg_type == "interactive" and locals().get("_interactive_button_id"):
                        enqueue_payload["button_id"] = _interactive_button_id
                    inserted = await inbox_repo.enqueue(
                        pool,
                        provider="meta_whatsapp",
                        external_id=external_id,
                        payload=enqueue_payload,
                    )
                    if not inserted:
                        log.info("inbox_dedup_skipped", wam_id=wam_id, user_phone=user_phone)
                except (PoolAcquireTimeout, ConnectionError) as _db_exc:
                    log.error(
                        "chat.enqueue_failed_db_down",
                        wam_id=wam_id,
                        user_phone=user_phone,
                        exc=str(_db_exc),
                    )
                    any_enqueue_failed = True
                    # DB-connectivity failure — classified as db_down
                    continue
                except Exception:
                    log.exception("chat.enqueue_failed", wam_id=wam_id, user_phone=user_phone)
                    any_enqueue_failed = True
                    all_failures_are_db_down = False  # non-DB failure
                    continue

        except Exception:
            log.exception("chat.webhook_critical_error")

    # 9. ACK inmediato a Meta (<200ms) — evita reintentos.
    #
    # DESIGN DECISION — why we return 200 even when enqueues fail due to DB down:
    #
    # Returning 503 tells Meta "delivery failed, please retry".  Meta will then
    # re-send the SAME batch up to 5 times over 5 minutes.  During a DB outage
    # that means each incoming message multiplies into 5 attempts, turning a
    # short blip into a sustained flood that makes recovery even harder.
    #
    # Returning 200 tells Meta "received, do not retry".  We lose the batch for
    # that window, but the inbox worker will naturally catch up any messages that
    # survive the outage (e.g. those with durable WAM IDs that Meta re-delivers
    # on the next conversation turn).  The trade-off is justified: a partial
    # message loss during an outage is vastly preferable to a retry avalanche.
    #
    # We ONLY return 503 for non-DB failures (unexpected errors unrelated to
    # connectivity) where a retry might actually succeed.
    if any_enqueue_failed and not all_failures_are_db_down:
        return JSONResponse(content={"status": "partial_failure"}, status_code=503)
    if any_enqueue_failed:
        log.warning("meta_webhook.enqueue_all_failed_db_down")
    return JSONResponse(content={"status": "ok"}, status_code=200)

@router.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = await request.form()
    user_message = form.get("Body", "")
    user_phone = form.get("From", "").replace("whatsapp:", "")
    raw_bot_number = form.get("To", "").replace("whatsapp:", "")
    bot_number = _normalize_number(raw_bot_number)

    if not user_message or not user_phone:
        return Response(content="", media_type="application/xml")

    result = await chat(user_phone, user_message, bot_number)

    if result is None: return Response(content="", media_type="application/xml")
    twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{result['message']}</Message></Response>"
    return Response(content=twiml, media_type="application/xml")