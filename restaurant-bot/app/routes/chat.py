from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation
from app.data.restaurant import get_reservations

router = APIRouter()

# ─────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────

class ChatRequest(BaseModel):
    phone: str
    message: str

class ResetRequest(BaseModel):
    phone: str


# ─────────────────────────────────────────────
# ENDPOINT PRINCIPAL - Chat directo (testing)
# ─────────────────────────────────────────────

@router.post("/chat")
async def chat_endpoint(request: ChatRequest):
    """
    Endpoint para probar el bot directamente.
    En producción esto lo llama el webhook de Twilio/Meta.
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")
    
    result = chat(request.phone, request.message)
    return {
        "success": True,
        "phone": request.phone,
        "response": result["message"],
        "actions": result["actions"]
    }


# ─────────────────────────────────────────────
# WEBHOOK DE TWILIO (WhatsApp real)
# ─────────────────────────────────────────────

@router.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    """
    Recibe mensajes de WhatsApp via Twilio.
    Configura este URL en tu Twilio Console.
    """
    form_data = await request.form()
    
    user_phone = form_data.get("From", "").replace("whatsapp:", "")
    user_message = form_data.get("Body", "")
    
    if not user_phone or not user_message:
        raise HTTPException(status_code=400, detail="Datos inválidos de Twilio")
    
    result = chat(user_phone, user_message)
    
    # Respuesta en formato TwiML para Twilio
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>
        <Body>{result['message']}</Body>
    </Message>
</Response>"""
    
    # Log de acciones (en producción enviar a sistema de notificaciones)
    for action in result["actions"]:
        if action["type"] == "escalate_to_human":
            print(f"⚠️  ESCALAR A HUMANO - Tel: {user_phone} - Motivo: {action['reason']}")
        elif action["type"] == "reservation_created":
            print(f"✅ RESERVACIÓN CREADA - {action['data']}")
    
    from fastapi.responses import Response
    return Response(content=twiml_response, media_type="application/xml")


# ─────────────────────────────────────────────
# WEBHOOK DE META (WhatsApp Business API oficial)
# ─────────────────────────────────────────────

@router.get("/webhook/meta")
async def meta_webhook_verify(request: Request):
    """Verificación del webhook de Meta."""
    params = dict(request.query_params)
    verify_token = "MI_TOKEN_SECRETO"  # Cambiar por variable de entorno
    
    if params.get("hub.verify_token") == verify_token:
        return int(params.get("hub.challenge", 0))
    raise HTTPException(status_code=403, detail="Token inválido")

@router.post("/webhook/meta")
async def meta_webhook(request: Request):
    """Recibe mensajes de WhatsApp via Meta Cloud API."""
    body = await request.json()
    
    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        
        if "messages" not in value:
            return {"status": "no_message"}
        
        message = value["messages"][0]
        user_phone = message["from"]
        user_message = message["text"]["body"]
        
        result = chat(user_phone, user_message)
        
        # En producción aquí enviarías la respuesta via Meta API
        print(f"📱 Respuesta para {user_phone}: {result['message']}")
        
        return {"status": "ok", "response": result["message"]}
    
    except (KeyError, IndexError):
        return {"status": "ignored"}


# ─────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────

@router.get("/reservations")
async def list_reservations():
    """Ver todas las reservaciones."""
    return {"reservations": get_reservations(), "total": len(get_reservations())}

@router.post("/reset")
async def reset_chat(request: ResetRequest):
    """Reinicia la conversación de un usuario."""
    reset_conversation(request.phone)
    return {"success": True, "message": f"Conversación de {request.phone} reiniciada"}
