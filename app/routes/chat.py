import os
import httpx
import traceback
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation

router = APIRouter()

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
    if mode == "subscribe" and token == os.getenv("META_VERIFY_TOKEN", "mesio_secret_2024"):
        return Response(content=challenge)
    return Response(content="Error de verificacion", status_code=403)

@router.post("/webhook/meta")
async def meta_webhook(request: Request):
    data = await request.json()
    print("\n--- 📥 NUEVO MENSAJE DE META ---", flush=True)
    
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        
        if "messages" not in value: return {"status": "ok"}
            
        message = value.get("messages", [{}])[0]
        
        if message:
            user_phone = message.get("from")
            msg_type = message.get("type", "text")
            raw_bot_number = value.get("metadata", {}).get("display_phone_number", "")
            bot_number = _normalize_number(raw_bot_number)
            phone_id = value.get("metadata", {}).get("phone_number_id", "")

            if msg_type == "location":
                loc = message.get("location", {})
                lat = loc.get("latitude")
                lon = loc.get("longitude")
                user_text = f"Mi ubicacion es lat:{lat} lon:{lon}. Quiero hacer un pedido de domicilio."
            else:
                user_text = message.get("text", {}).get("body", "")

            if not user_text: return {"status": "ok"}

            print(f"💬 De: {user_phone} | Para Bot: {bot_number} | ID: {phone_id}", flush=True)
            print(f"📝 Texto: {user_text}", flush=True)

            print("🧠 Pensando respuesta con IA...", flush=True)
            # LLAMADA CORRECTA CON LOS 3 ARGUMENTOS
            result = await chat(user_phone, user_text, bot_number)
            print(f"🤖 Resultado IA: {result}", flush=True)

            if result and result.get("message"):
                async with httpx.AsyncClient(timeout=10) as client:
                    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
                    headers = {"Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN', '')}"}
                    res = await client.post(url, headers=headers, json={
                        "messaging_product": "whatsapp",
                        "to": user_phone,
                        "type": "text",
                        "text": {"body": result["message"]}
                    })
                    print(f"📤 Meta Status: {res.status_code}", flush=True)
                    if res.status_code != 200:
                        print(f"🚨 ERROR META: {res.text}", flush=True)

    except Exception as e:
        print(f"❌ ERROR CRÍTICO EN WEBHOOK:\n{traceback.format_exc()}", flush=True)

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
        
    # LLAMADA CORRECTA CON LOS 3 ARGUMENTOS
    result = await chat(user_phone, user_message, bot_number)
    
    if result is None: return Response(content="", media_type="application/xml")
    twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{result['message']}</Message></Response>"
    return Response(content=twiml, media_type="application/xml")