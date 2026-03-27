import os
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.routes.deps import require_auth, get_current_restaurant

_NPS_INTERNAL_KEY = os.getenv("NPS_INTERNAL_KEY", "")

router = APIRouter()


class NPSResponse(BaseModel):
    phone: str
    bot_number: str
    score: int
    comment: str = ""


@router.post("/api/nps/response")
async def save_nps_response(request: Request, body: NPSResponse):
    """Internal endpoint: saves NPS response. Called only from the agent service."""
    key = request.headers.get("X-Internal-Key", "")
    if not _NPS_INTERNAL_KEY or key != _NPS_INTERNAL_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if body.score < 1 or body.score > 5:
        raise HTTPException(status_code=400, detail="Score must be between 1 and 5")
    await db.db_save_nps_response(
        phone=body.phone,
        bot_number=body.bot_number,
        score=body.score,
        comment=body.comment,
    )
    return {"success": True}


@router.get("/api/nps/stats")
async def get_nps_stats(request: Request, period: str = "month"):
    """Estadísticas NPS filtradas por sucursal exacta"""
    restaurant = await get_current_restaurant(request)
    
    # 🛡️ LEER SELECTOR GLOBAL: Obtenemos el bot_number real de la sucursal seleccionada
    # (get_current_restaurant ya maneja la lógica de X-Branch-ID internamente)
    bot_number = restaurant["whatsapp_number"]
    
    stats = await db.db_get_nps_stats(bot_number, period)
    return stats


@router.get("/api/nps/responses")
async def get_nps_responses(request: Request, period: str = "month", limit: int = 50):
    """Lista de respuestas NPS filtrada por sucursal exacta"""
    restaurant = await get_current_restaurant(request)
    
    # 🛡️ LEER SELECTOR GLOBAL
    bot_number = restaurant["whatsapp_number"]
    
    responses = await db.db_get_nps_responses(bot_number, period, limit)
    return {"responses": responses}

@router.get("/api/nps/google-maps-url")
async def get_google_maps_url(request: Request):
    """Retorna la URL de Google Maps del restaurante"""
    restaurant = await get_current_restaurant(request)
    maps_url = restaurant.get("google_maps_url", "")
    return {"url": maps_url}


@router.post("/api/nps/google-maps-url")
async def set_google_maps_url(request: Request):
    """Guarda la URL de Google Maps del restaurante"""
    await require_auth(request)
    restaurant = await get_current_restaurant(request)
    body = await request.json()
    url = body.get("url", "").strip()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
    "UPDATE restaurants SET google_maps_url = $1 WHERE id = $2",
    url,
    restaurant["id"]
)
    return {"success": True}