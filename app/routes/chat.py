import os
import hmac
import hashlib
import asyncio
import httpx
import traceback
from collections import defaultdict
from time import time
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation

router = APIRouter()

# ── RATE LIMITING (V-05) ─────────────────────────────────────────────
# In-memory rate limiter por número de teléfono
# {phone: [timestamp, ...]}  (ventana deslizante de 60s)
_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MESSAGES = 20   # max mensajes por ventana
RATE_LIMIT_WINDOW   = 60   # segundos

def _is_rate_limited(phone: str) -> bool:
    now = time()
    window_start = now - RATE_LIMIT_WINDOW
    # Limpiar timestamps viejos
    _rate_store[phone] = [t for t in _rate_store[phone] if t > window_start]
    if len(_rate_store[phone]) >= RATE_LIMIT_MESSAGES:
        return True
    _rate_store[phone].append(now)
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


class ChatRequest(BaseModel):
    phone: str
    message: str
    bot_number: str = "15556293573"


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


@router.get("/webhook/meta")
async def verify_meta_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    verify_token = os.getenv("META_VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN", "mesio_secret_2024")
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
            url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
            headers = {"Authorization": f"Bearer {access_token}"}
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.post(url, headers=headers, json={
                    "messaging_product": "whatsapp",
                    "to": user_phone,
                    "type": "text",
                    "text": {"body": result["message"]}
                })
                print(f"📤 Meta Status: {res.status_code}", flush=True)
                if res.status_code != 200:
                    print(f"🚨 ERROR META: {res.text}", flush=True)
    except Exception:
        print(f"❌ ERROR en _process_message:\n{traceback.format_exc()}", flush=True)


@router.post("/webhook/meta")
async def meta_webhook(request: Request):
    # 1. Leer body ANTES de parsear JSON (necesitamos bytes para la firma)
    raw_body = await request.body()

    # 2. Verificar firma Meta (V-02)
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_meta_signature(raw_body, signature):
        return Response(content="Unauthorized", status_code=401)

    # 3. ACK inmediato a Meta (V-03) — evita reintento por timeout
    # El procesamiento ocurre en background
    try:
        import json as _json
        data = _json.loads(raw_body)
    except Exception:
        return {"status": "ok"}

    print("\n--- 📥 NUEVO MENSAJE DE META ---", flush=True)

    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        if "messages" not in value:
            return {"status": "ok"}

        message = value.get("messages", [{}])[0]
        if not message:
            return {"status": "ok"}

        user_phone = message.get("from", "")
        msg_type = message.get("type", "text")
        raw_bot_number = value.get("metadata", {}).get("display_phone_number", "")
        bot_number = _normalize_number(raw_bot_number)
        phone_id = value.get("metadata", {}).get("phone_number_id", "")
        access_token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")

        # 4. Rate limiting (V-05)
        if user_phone and _is_rate_limited(user_phone):
            print(f"🚫 Rate limit: {user_phone}", flush=True)
            return {"status": "ok"}

        if msg_type == "location":
            loc = message.get("location", {})
            lat = loc.get("latitude")
            lon = loc.get("longitude")
            user_text = f"Mi ubicacion es lat:{lat} lon:{lon}. Quiero hacer un pedido de domicilio."
        else:
            user_text = message.get("text", {}).get("body", "")

        if not user_text or not user_phone:
            return {"status": "ok"}

        print(f"💬 De: {user_phone} | Para Bot: {bot_number} | ID: {phone_id}", flush=True)
        print(f"📝 Texto: {user_text[:200]}", flush=True)  # Limitar log a 200 chars

        crm_phone_id = os.getenv("CRM_PHONE_NUMBER_ID")
        if crm_phone_id and phone_id == crm_phone_id:
            from app.routes.crm import register_inbound_from_prospect
            wa_msg_id = message.get("id", "")
            # Registra la interacción en el CRM de prospectos
            asyncio.create_task(register_inbound_from_prospect(user_phone, user_text, wa_msg_id))
            print("👤 Mensaje enrutado al CRM (no se activa IA)", flush=True)
            return {"status": "ok"}

        # 5. Disparar background task (V-03 fix: no bloqueamos el handler)
        asyncio.create_task(
            _process_message(user_phone, user_text, bot_number, phone_id, access_token)
        )

    except Exception:
        print(f"❌ ERROR CRÍTICO EN WEBHOOK:\n{traceback.format_exc()}", flush=True)

    # 6. Retornar 200 inmediatamente — Meta no reintenta
    print("--------------------------------\n", flush=True)
    return {"status": "ok"}


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