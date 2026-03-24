"""
Shared FastAPI dependencies for route authentication and restaurant resolution.
Import these instead of redefining per-router auth helpers.
"""
from fastapi import Request, HTTPException
from app.services.auth import verify_token
from app.services import database as db


async def require_auth(request: Request) -> str:
    """Validates Bearer token; returns username or raises 401."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return username


async def get_current_user(request: Request) -> dict:
    """Returns the authenticated user dict or raises 401."""
    username = await require_auth(request)
    user = await db.db_get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def get_current_restaurant(request: Request) -> dict:
    """Returns the restaurant for the authenticated user or raises 403."""
    user = await get_current_user(request)
    if user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r:
            return r
    all_r = await db.db_get_all_restaurants()
    if all_r:
        return all_r[0]
    raise HTTPException(status_code=403, detail="Restaurant not found")
