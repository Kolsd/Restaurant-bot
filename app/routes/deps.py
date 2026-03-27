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

    # Si es un empleado operativo, lo buscamos en la tabla staff
    if username.startswith("staff:"):
        staff_id = username.split(":", 1)[1]
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            # Usamos id::text para evitar conflictos de tipo UUID/INT
            query = "SELECT restaurant_id, role FROM staff WHERE id::text = $1"
            staff_member = await conn.fetchrow(query, str(staff_id))
            if staff_member:
                # Construimos un dict compatible con lo que espera el sistema
                return {
                    "username": username,
                    "branch_id": staff_member["restaurant_id"],
                    "role": staff_member["role"]
                }
    else:
        # Si es un admin/gerente normal, lo buscamos en users
        user = await db.db_get_user(username)
        if user:
            return user

    raise HTTPException(status_code=401, detail="User not found")

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

    Reads features directly from the already-loaded restaurant dict to avoid
    a second DB round-trip and normalisation mismatches in db_check_module.
    Accepts both boolean True and the string "true" as enabled values.

    Raises:
        401 — if the Bearer token is missing or invalid (via get_current_restaurant).
        403 — if the restaurant exists but does not have the module enabled.
    """
    import json as _json

    async def _check_module(
        restaurant: dict = Depends(get_current_restaurant),
    ) -> None:
        features = restaurant.get("features") or {}
        if isinstance(features, str):
            try:
                features = _json.loads(features)
            except Exception:
                features = {}
        if not isinstance(features, dict):
            features = {}
        val = features.get(module_name)
        has_module = val is True or str(val).lower() == "true"
        if not has_module:
            raise HTTPException(
                status_code=403,
                detail=f"El restaurante no tiene activo el módulo: {module_name}",
            )

    return _check_module
