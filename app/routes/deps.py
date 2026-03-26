"""
Shared FastAPI dependencies for route authentication and restaurant resolution.
Import these instead of redefining per-router auth helpers.
"""
from typing import Callable
from fastapi import Request, HTTPException, Depends
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


def require_module(module_name: str) -> Callable:
    """
    FastAPI dependency factory for module-level access control.

    Usage in a router:
        @router.get("/endpoint", dependencies=[Depends(require_module("staff_tips"))])
        async def my_endpoint(): ...

    Or combined with get_current_restaurant to reuse the resolved restaurant:
        async def my_endpoint(
            restaurant: dict = Depends(get_current_restaurant),
            _: None = Depends(require_module("staff_tips")),
        ): ...

    Raises:
        401 — if the Bearer token is missing or invalid (via get_current_restaurant).
        403 — if the restaurant exists but does not have the module enabled.
    """
    async def _check_module(
        restaurant: dict = Depends(get_current_restaurant),
    ) -> None:
        bot_number = restaurant.get("whatsapp_number", "")
        has_module = await db.db_check_module(bot_number, module_name)
        if not has_module:
            raise HTTPException(
                status_code=403,
                detail=f"El restaurante no tiene activo el módulo: {module_name}",
            )

    return _check_module
