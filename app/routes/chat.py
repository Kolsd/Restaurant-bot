import os
import hmac
import hashlib
import httpx
import traceback
from collections import defaultdict
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation
from app.services import database as db
from app.repositories import inbox_repo
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

# ── RATE LIMITING BACKED BY POSTGRES (Workers Safe) ──────────────────
RATE_LIMIT_MESSAGES = 20   # max mensajes por ventana
RATE_LIMIT_WINDOW   = 60   # segundos

async def _is_rate_limited(phone: str) -> bool:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        # 1. Borrar el historial viejo de este número (mantiene la tabla liviana)
        await conn.execute(
            f"DELETE FROM meta_rate_limits WHERE phone = $1 AND created_at < NOW() - INTERVAL '{RATE_LIMIT_WINDOW} seconds'",
            phone
        )

        # 2. Contar cuántos mensajes ha enviado en los últimos N segundos
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM meta_rate_limits WHERE phone = $1",
            phone
        )

        if count >= RATE_LIMIT_MESSAGES:
            return True

        await conn.execute("INSERT INTO meta_rate_limits (phone) VALUES ($1)", phone)
        return False

# ── META SIGNATURE VERIFICATION (V-02) ──────────────────────────────
def _verify_meta_signature(body: bytes, signature_header: str) -> bool:
    """Verifica X-Hub-Signature-256 de Meta para autenticar el webhook."""
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_secret:
        # Si no hay secret configurado, logueamos advertencia pero dejamos pasar
        # (para no romper instancias en dev que no lo tengan aún)
        print("⚠️  META_APP_SECRET no configurado — verificación de firma desactivada", flush=True)
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        print("🚨 Webhook rechazado: sin firma X-Hub-Signature-256", flush=True)
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
async def get_whatsapp_media(media_id: str, bot: str = ""):
    """Descarga la imagen encriptada desde Meta y la muestra en el navegador del Cajero"""
    rest = await db.db_get_restaurant_by_phone(bot)
    token = rest.get("wa_access_token") if rest else os.getenv("META_ACCESS_TOKEN")
    if not token:
        token = os.getenv("WHATSAPP_TOKEN", "")

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
async def _process_message(user_phone: str, user_text: str, bot_number: str,
                            phone_id: str, access_token: str):
    """Procesamiento real de la IA — corre en background, desacoplado del ACK."""
    try:
        result = await chat(user_phone, user_text, bot_number, meta_phone_id=phone_id)
        print(f"🤖 Resultado IA: {result}", flush=True)

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
                print(f"📤 Meta Status: {res.status_code}", flush=True)
                if res.status_code != 200:
                    print(f"🚨 ERROR META: {res.text}", flush=True)
    except Exception:
        print(f"❌ ERROR en _process_message:\n{traceback.format_exc()}", flush=True)


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
                print(f"🚨 _send_wa_text ERROR: {res.text}", flush=True)
    except Exception:
        print(f"❌ ERROR en _send_wa_text:\n{traceback.format_exc()}", flush=True)


@router.post("/webhook/meta")
async def meta_webhook(request: Request, background_tasks: BackgroundTasks):
    import json as _json

    # 1. Leer body ANTES de parsear JSON (necesitamos bytes para la firma)
    raw_body = await request.body()

    # 2. Verificar firma Meta
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_meta_signature(raw_body, signature):
        return Response(content="Unauthorized", status_code=401)

    try:
        data = _json.loads(raw_body)
    except Exception:
        return JSONResponse(content={"status": "ok"})

    print("\n--- 📥 NUEVO MENSAJE DE META ---", flush=True)

    try:
        entry   = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})

        if "messages" not in value:
            return JSONResponse(content={"status": "ok"})

        message = value.get("messages", [{}])[0]
        if not message:
            return JSONResponse(content={"status": "ok"})

        # 3. Deduplicación por WAM_ID — descarta reintentos de Meta
        wam_id = message.get("id", "")
        if wam_id and await db.db_is_duplicate_wam(wam_id):
            print(f"⚡ WAM duplicado ignorado: {wam_id}", flush=True)
            return JSONResponse(content={"status": "ok"})

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
                pool_tmp = await db.get_pool()
                async with pool_tmp.acquire() as _conn:
                    await _conn.execute(
                        "UPDATE restaurants SET wa_phone_id=$1 WHERE id=$2",
                        phone_id, restaurant["id"],
                    )
            except Exception:
                pass  # non-critical, next message will retry

        # 5. Ruta CRM — procesamiento inline (no requiere IA)
        crm_phone_id = os.getenv("CRM_PHONE_NUMBER_ID")
        if crm_phone_id and phone_id == crm_phone_id:
            from app.routes.crm import register_inbound_from_prospect
            if msg_type == "location":
                loc = message.get("location", {})
                user_text = f"📍 Ubicación compartida: lat:{loc.get('latitude')} lon:{loc.get('longitude')}"
            else:
                user_text = message.get("text", {}).get("body", "")
            if user_text and user_phone:
                print(f"💬 [CRM Inbound] De: {user_phone} | ID: {phone_id}", flush=True)
                await register_inbound_from_prospect(user_phone, user_text, wam_id)
                print("👤 Mensaje del CRM guardado en BD exitosamente", flush=True)
            return JSONResponse(content={"status": "ok"})

        # 6. Rate limiting
        is_limited = await _is_rate_limited(user_phone)
        if user_phone and is_limited:
            print(f"🚫 Rate limit activado para: {user_phone}", flush=True)
            return JSONResponse(content={"status": "ok"})

        # 7. Extraer texto del mensaje
        # 7. Extraer texto del mensaje
        if msg_type == "location":
            loc = message.get("location", {})
            lat, lon = loc.get("latitude"), loc.get("longitude")
            
            try:
                cart = await db.db_get_cart(user_phone, bot_number)
                cart["latitude"] = lat
                cart["longitude"] = lon
                await db.db_save_cart(user_phone, bot_number, cart)
            except Exception as e:
                print(f"Error guardando GPS en carrito: {e}")
                
            maps_url = f"https://maps.google.com/?q={lat},{lon}"
            user_text = f"Mi ubicación es: {maps_url} (lat:{lat}, lon:{lon}). Quiero hacer un pedido de domicilio."
        elif msg_type == "interactive":
            button_reply = message.get("interactive", {}).get("button_reply", {})
            user_text = button_reply.get("id", "") or button_reply.get("title", "")
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
                        return JSONResponse(content={"status": "ok"})
            except Exception as e:
                print(f"⚠️ Error en atajo de comprobante: {e}", flush=True)

            user_text = f"📸 [IMAGEN RECIBIDA] Link del comprobante: {media_url}"
        else:
            user_text = message.get("text", {}).get("body", "")

        if not user_text or not user_phone:
            return JSONResponse(content={"status": "ok"})

        print(f"💬 [Bot Inbound] De: {user_phone} | Bot: {bot_number} | WAM: {wam_id}", flush=True)
        print(f"📝 Texto: {user_text[:200]}", flush=True)

        # 8. Persist to webhook_inbox — durable processing survives worker restarts.
        #    The inbox worker (inbox_worker.py) will call _process_message asynchronously.
        pool = await db.get_pool()
        enqueue_payload = {
            "user_phone":   user_phone,
            "user_text":    user_text,
            "bot_number":   bot_number,
            "phone_id":     phone_id,
            "access_token": access_token,
        }
        inserted = await inbox_repo.enqueue(
            pool,
            provider="meta_whatsapp",
            external_id=wam_id or None,
            payload=enqueue_payload,
        )
        if not inserted:
            log.info("inbox_dedup_skipped", wam_id=wam_id, user_phone=user_phone)

    except Exception:
        print(f"❌ ERROR CRÍTICO EN WEBHOOK:\n{traceback.format_exc()}", flush=True)

    # 9. ACK inmediato a Meta (<200ms) — evita reintentos
    print("✅ 200 OK enviado a Meta\n--------------------------------\n", flush=True)
    return JSONResponse(content={"status": "received"})

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