import httpx
import os
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation

router = APIRouter()


def _normalize_number(number: str) -> str:
    """
    Normaliza números de WhatsApp:
    - Elimina espacios
    - Elimina el prefijo '+'
    """
    if not number:
        return ""
    return number.replace(" ", "").replace("+", "")

class ChatRequest(BaseModel):
    phone: str
    message: str

class ResetRequest(BaseModel):
    phone: str

@router.post("/chat")
async def chat_endpoint(request: ChatRequest):
    # Endpoint para pruebas desde el dashboard o terminal
    result = await chat(request.phone, request.message, "TEST_BOT")
    return {"success": True, "response": result["message"]}

@router.post("/reset")
async def reset_chat(request: ResetRequest):
    await reset_conversation(request.phone)
    return {"success": True, "message": f"Conversación de {request.phone} reiniciada"}

# ── WEBHOOK PARA META (WHATSAPP CLOUD API) ──

@router.get("/webhook/meta")
async def verify_meta_webhook(request: Request):
    # Verificación inicial que pide Meta
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    
    if mode == "subscribe" and token == os.getenv("META_VERIFY_TOKEN", "mesio_secret_2024"):
        return Response(content=challenge)
    return Response(content="Error de verificación", status_code=403)

@router.post("/webhook/meta")
async def meta_webhook(request: Request):
    data = await request.json()
    
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        message = value.get("messages", [{}])[0]
        
        if message:
            user_phone = message.get("from")
            user_text = message.get("text", {}).get("body", "")
            raw_bot_number = value.get("metadata", {}).get("display_phone_number")
            bot_number = _normalize_number(raw_bot_number)
            phone_id = value.get("metadata", {}).get("phone_number_id")

            # 1. Procesar respuesta con nuestra IA
            result = await chat(user_phone, user_text, bot_number)
            
            # 2. Enviar respuesta de vuelta a WhatsApp vía Meta API
            if result and result.get("message"):
                async with httpx.AsyncClient() as client:
                    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
                    headers = {"Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN')}"}
                    json_data = {
                        "messaging_product": "whatsapp",
                        "to": user_phone,
                        "type": "text",
                        "text": {"body": result["message"]}
                    }
                    await client.post(url, headers=headers, json=json_data)

    except Exception as e:
        print(f"Error procesando Webhook Meta: {e}")

    return {"status": "ok"}

# ── WEBHOOK PARA TWILIO (OPCIONAL) ──

@router.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = await request.form()
    user_message = form.get("Body", "")
    user_phone = form.get("From", "").replace("whatsapp:", "")
    bot_number = form.get("To", "").replace("whatsapp:", "")
    result = await chat(user_phone, user_message, bot_number)
    
    if result is None: return Response(content="", media_type="application/xml")
    
    twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{result['message']}</Message></Response>"
    return Response(content=twiml, media_type="application/xml")
