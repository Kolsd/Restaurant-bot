import os
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.routes.deps import require_auth, get_current_restaurant, get_current_user

_NPS_INTERNAL_KEY = os.getenv("NPS_INTERNAL_KEY", "")

router = APIRouter()

class NPSResponse(BaseModel):
    phone: str
    bot_number: str
    score: int
    comment: str = ""

def _resolve_branch_id(request: Request, user: dict, restaurant: dict):
    branch_header = request.headers.get("X-Branch-ID")
    is_admin = any(r in user.get("role", "") for r in ["owner", "admin"])
    
    if is_admin:
        if branch_header == "all": return "all"
        elif branch_header == "matriz": return None
        elif branch_header and branch_header.isdigit(): return int(branch_header)
        return None
    return user.get("branch_id") or (restaurant["id"] if restaurant.get("parent_restaurant_id") else None)
    
@router.post("/api/nps/response")
async def save_nps_response(request: Request, body: NPSResponse):
    key = request.headers.get("X-Internal-Key", "")
    if not _NPS_INTERNAL_KEY or key != _NPS_INTERNAL_KEY: raise HTTPException(403)
    if body.score < 1 or body.score > 5: raise HTTPException(400)
    await db.db_save_nps_response(body.phone, body.bot_number, body.score, body.comment)
    return {"success": True}

@router.get("/api/nps/stats")
async def get_nps_stats(request: Request, period: str = "month"):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    branch_id = _resolve_branch_id(request, user, restaurant)
    raw_bot_num = restaurant.get("whatsapp_number", "")
    clean_bot_num = raw_bot_num.split("_b")[0] if raw_bot_num else ""
    return await db.db_get_nps_stats(clean_bot_num, period, branch_id=branch_id)
    
@router.get("/api/nps/responses")
async def get_nps_responses(request: Request, period: str = "month", limit: int = 50):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    branch_id = _resolve_branch_id(request, user, restaurant)
    raw_bot_num = restaurant.get("whatsapp_number", "")
    clean_bot_num = raw_bot_num.split("_b")[0] if raw_bot_num else ""
    return {"responses": await db.db_get_nps_responses(clean_bot_num, period, limit, branch_id=branch_id)}

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
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        import json
        await conn.execute("UPDATE restaurants SET features = $1::jsonb WHERE id = $2", json.dumps(features), restaurant["id"])
    return {"success": True, "url": url}