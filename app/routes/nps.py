import os
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.repositories import conversations_repo
from app.routes.deps import (
    require_auth, get_current_restaurant, get_current_user, resolve_sede_filter,
)
from app.services.tenant_context import tenant_scope

_NPS_INTERNAL_KEY = os.getenv("NPS_INTERNAL_KEY", "")

router = APIRouter()

class NPSResponse(BaseModel):
    phone: str
    bot_number: str
    score: int
    comment: str = ""

def _resolve_branch_id(request: Request, user: dict, restaurant: dict):
    """Which sede's NPS the caller may read.

    Fixed 2026-09-20: the non-admin branch returned `user["branch_id"]`,
    which on a staff row is the ORG id rather than a sede — so it either
    matched nothing or handed a waiter the whole org's ratings. The shared
    resolver in app/routes/deps.py returns their real `location_id`."""
    return resolve_sede_filter(request, user, allow_all_sentinel=True)
    
@router.post("/api/nps/response")
async def save_nps_response(request: Request, body: NPSResponse):
    key = request.headers.get("X-Internal-Key", "")
    if not _NPS_INTERNAL_KEY or key != _NPS_INTERNAL_KEY: raise HTTPException(403)
    if body.score < 1 or body.score > 5: raise HTTPException(400)
    await db.db_save_nps_response(body.phone, body.bot_number, body.score, body.comment)
    return {"success": True}

@router.get("/api/nps/stats")
async def get_nps_stats(request: Request, period: str = "month", days: int = None):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    branch_id = _resolve_branch_id(request, user, restaurant)
    bot_number = restaurant.get("whatsapp_number") or ""  # the org's bot key
    return await db.db_get_nps_stats(bot_number, period, branch_id=branch_id, days=days)
    
@router.get("/api/nps/responses")
async def get_nps_responses(request: Request, period: str = "month", limit: int = 50):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    branch_id = _resolve_branch_id(request, user, restaurant)
    bot_number = restaurant.get("whatsapp_number") or ""  # the org's bot key
    return {"responses": await db.db_get_nps_responses(bot_number, period, limit, branch_id=branch_id)}

@router.get("/api/nps/google-maps-url")
async def get_google_maps_url(request: Request):
    return {"url": (await get_current_restaurant(request)).get("google_maps_url", "")}

@router.post("/api/nps/google-maps-url")
async def set_google_maps_url(request: Request):
    await require_auth(request)
    restaurant = await get_current_restaurant(request)
    url = (await request.json()).get("url", "")
    features = restaurant.get("features", {})
    if isinstance(features, str):
        import json; features = json.loads(features)
    features["google_maps_url"] = url
    with tenant_scope(restaurant["id"]):
        await conversations_repo.db_update_restaurant_features(restaurant["id"], features)
    return {"success": True, "url": url}