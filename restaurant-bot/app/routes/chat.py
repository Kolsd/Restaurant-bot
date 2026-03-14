from fastapi import APIRouter, Request
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation
from app.data.restaurant import reservations

router = APIRouter()


class ChatRequest(BaseModel):
    phone: str
    message: str

class ResetRequest(BaseModel):
    phone: str


@router.post("/chat")
async def chat_endpoint(request: ChatRequest):
    result = await chat(request.phone, request.message)
    return {"success": True, "phone": request.phone, "response": result["message"], "actions": result["actions"]}


@router.post("/reset")
async def reset_chat(request: ResetRequest):
    await reset_conversation(request.phone)
    return {"success": True, "message": f"Conversación de {request.phone} reiniciada"}


@router.get("/reservations")
async def list_reservations():
    from app.services.database import db_get_all_reservations
    return {"reservations": await db_get_all_reservations()}


@router.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = await request.form()
    user_message = form.get("Body", "")
    user_phone = form.get("From", "").replace("whatsapp:", "")

    if not user_message or not user_phone:
        return {"error": "Mensaje o teléfono vacío"}

    result = await chat(user_phone, user_message)

    twilio_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{result['message']}</Message></Response>"""
    from fastapi.responses import Response
    return Response(content=twilio_response, media_type="application/xml")
