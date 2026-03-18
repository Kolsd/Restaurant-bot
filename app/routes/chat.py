import os
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from app.services.agent import chat, reset_conversation

router = APIRouter()


def _normalize_number(number: str) -> str:
    if not number:
        return ""
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
    if result is None:
        return {"success": True, "response": ""}
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
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        message = value.get("messages", [{}])[0]

        if message:
            user_phone = message.get("from")
            msg_type = message.get("type", "text")
            raw_bot_number = value.get("metadata", {}).get("display_phone_number", "")
            bot_number = _normalize_number(raw_bot_number)
            phone_id = value.get("metadata", {}).get("phone_number_id", "")

            # Handle location messages
            if msg_type == "location":
                loc = message.get("location", {})
                lat = loc.get("latitude")
                lon = loc.get("longitude")
                if lat and lon:
                    try:
                        from app.services.agent import find_nearest_branch
                        branch, dist_km = await find_nearest_branch(lat, lon)
                        if branch:
                            dist_text = f"{dist_km:.1f} km" if dist_km and dist_km < 50 else "disponible"
                            user_text = (f"Mi ubicacion es lat:{lat} lon:{lon}. "
                                        f"La sucursal mas cercana es {branch['name']} a {dist_text}. "
                                        f"Quiero hacer un pedido de domicilio a esta sucursal.")
                        else:
                            user_text = f"Mi ubicacion es lat:{lat} lon:{lon}. Quiero hacer un pedido de domicilio."
                    except Exception:
                        user_text = f"Mi ubicacion es lat:{lat} lon:{lon}. Quiero hacer un pedido."
                else:
                    user_text = "Quiero compartir mi ubicacion para el domicilio."
            else:
                user_text = message.get("text", {}).get("body", "")

            if not user_text:
                return {"status": "ok"}

            result = await chat(user_phone, user_text, bot_number)

            if result and result.get("message"):
                async with httpx.AsyncClient(timeout=10) as client:
                    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
                    headers = {"Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN', '')}"}
                    await client.post(url, headers=headers, json={
                        "messaging_product": "whatsapp",
                        "to": user_phone,
                        "type": "text",
                        "text": {"body": result["message"]}
                    })

    except Exception as e:
        print(f"Error webhook Meta: {e}")

    return {"status": "ok"}


@router.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = await request.form()
    user_message = form.get("Body", "")
    user_phone = form.get("From", "").replace("whatsapp:", "")
    bot_number = form.get("To", "").replace("whatsapp:", "")
    bot_number = _normalize_number(bot_number)
    if not user_message or not user_phone:
        return Response(content="", media_type="application/xml")
    result = await chat(user_phone, user_message, bot_number)
    if result is None:
        return Response(content="", media_type="application/xml")
    twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{result['message']}</Message></Response>"
    return Response(content=twiml, media_type="application/xml")
