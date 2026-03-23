import os
import httpx
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.services.auth import verify_token

router = APIRouter()

async def require_auth(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="No autorizado")
    return username

async def get_current_restaurant(request: Request) -> dict:
    username = await require_auth(request)
    user = await db.db_get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    if user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r:
            return r
    all_r = await db.db_get_all_restaurants()
    if all_r:
        return all_r[0]
    raise HTTPException(status_code=403, detail="Restaurante no encontrado")


class NPSResponse(BaseModel):
    phone: str
    bot_number: str
    score: int
    comment: str = ""


@router.post("/api/nps/response")
async def save_nps_response(body: NPSResponse):
    """Guarda la respuesta NPS del cliente (llamado desde el agent)"""
    if body.score < 1 or body.score > 5:
        raise HTTPException(status_code=400, detail="Score debe ser entre 1 y 5")
    await db.db_save_nps_response(
        phone=body.phone,
        bot_number=body.bot_number,
        score=body.score,
        comment=body.comment
    )
    return {"success": True}


@router.get("/api/nps/stats")
async def get_nps_stats(request: Request, period: str = "month"):
    """Estadísticas NPS para el dashboard"""
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    stats = await db.db_get_nps_stats(bot_number, period)
    return stats


@router.get("/api/nps/responses")
async def get_nps_responses(request: Request, period: str = "month", limit: int = 50):
    """Lista de respuestas NPS para el dashboard"""
    restaurant = await get_current_restaurant(request)
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