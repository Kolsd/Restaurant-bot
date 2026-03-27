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

    if username.startswith("staff:"):
        staff_id = username.split(":", 1)[1]
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            query = """
                SELECT s.restaurant_id, s.role, s.roles, r.parent_restaurant_id 
                FROM staff s
                JOIN restaurants r ON s.restaurant_id = r.id
                WHERE s.id::text = $1
            """
            staff_member = await conn.fetchrow(query, str(staff_id))

            if staff_member:
                is_main_restaurant = staff_member["parent_restaurant_id"] is None
                mapped_branch_id = None if is_main_restaurant else staff_member["restaurant_id"]

                raw_roles = staff_member["roles"]
                if isinstance(raw_roles, list):
                    roles_list = raw_roles
                elif isinstance(raw_roles, str):
                    import json as _j
                    try:
                        roles_list = _j.loads(raw_roles)
                    except Exception:
                        roles_list = []
                else:
                    roles_list = []

                if not roles_list and staff_member["role"]:
                    roles_list = [staff_member["role"]]

                combined_role = ",".join(roles_list) if roles_list else (staff_member["role"] or "")

                return {
                    "username": username,
                    "branch_id": mapped_branch_id,
                    "restaurant_id": staff_member["restaurant_id"],
                    "role": combined_role
                }

    user = await db.db_get_user(username)
    if user:
        return user

    raise HTTPException(status_code=401, detail="User not found")

async def get_current_restaurant(request: Request) -> dict:
    """Returns the restaurant for the authenticated user or raises 403."""
    user = await get_current_user(request)
    
    # 1. Si es Gerente de una sucursal específica
    if user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r:
            return r
            
    # 2. Si es Staff operativo (resuelve su restaurante principal exacto)
    if user.get("restaurant_id"):
        r = await db.db_get_restaurant_by_id(user["restaurant_id"])
        if r:
            return r
            
    # 3. Fallback para el Admin (toma el restaurante base)
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

# Al final del archivo, después de las funciones existentes

ROLE_PAGE_MAP = {
    "/mesero":      {"mesero"},
    "/caja":        {"caja", "cashier"},
    "/domiciliario":{"domiciliario", "delivery"},
    "/cocina":      {"cocina"},
    "/bar":         {"bar"},
    "/dashboard":   {"owner", "admin", "gerente"},
    "/settings":    {"owner", "admin", "gerente"},
    "/billing":     {"owner", "admin", "gerente"},
    "/staff":       {"owner", "admin", "gerente"},
}

ADMIN_ROLES = {"owner", "admin", "gerente"}

def _extract_roles(role_str: str) -> set:
    return {r.strip().lower() for r in (role_str or "").split(",") if r.strip()}

async def require_page_access(request: Request, path: str):
    """
    Verifica token + rol para servir una página HTML protegida.
    Redirige a /login si no hay token, a /staff si no tiene el rol.
    """
    from app.services.auth import verify_token
    from app.services import database as db

    token = None
    # Buscar token en cookie o header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
    # Las páginas HTML no mandan Authorization header — el token vive en localStorage
    # así que para rutas de página, devolvemos el HTML y dejamos que el JS valide
    # PERO: podemos leer una cookie si existe
    token = request.cookies.get("rb_token") or token

    allowed_roles = ROLE_PAGE_MAP.get(path, set())
    if not allowed_roles:
        return None  # ruta sin restricción definida, dejar pasar

    if not token:
        return None  # sin cookie, el JS en el HTML hará el redirect

    username = await verify_token(token)
    if not username:
        return None

    # Obtener rol del usuario
    if username.startswith("staff:"):
        staff_id = username.replace("staff:", "")
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role, roles FROM staff WHERE id=$1::uuid", staff_id
            )
        if not row:
            return None
        roles_list = row.get("roles") or []
        if not roles_list and row.get("role"):
            roles_list = [row["role"]]
        user_roles = {r.lower() for r in roles_list}
    else:
        user = await db.db_get_user(username)
        if not user:
            return None
        user_roles = _extract_roles(user.get("role", ""))

    # Admin siempre puede entrar a todo
    if user_roles & ADMIN_ROLES:
        return None  # permitir

    # Verificar si tiene algún rol permitido para esta página
    if not (user_roles & allowed_roles):
        raise HTTPException(status_code=403, detail="Rol no autorizado para esta página")

    return None