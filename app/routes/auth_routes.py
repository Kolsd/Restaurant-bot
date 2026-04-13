"""
Auth routes: login/logout, role verification, superadmin session management,
and superadmin restaurant/user CRUD.
"""
import os
import io
import base64
import json
import time
from collections import defaultdict
from fastapi import APIRouter, Request, HTTPException, File, UploadFile, Depends
from pydantic import BaseModel
from anthropic import Anthropic

from app.services.auth import login, logout, create_user, get_users, hash_password
from app.services import database as db
from app.routes.deps import require_auth, get_current_user, verify_superadmin
from app.repositories import sessions_repo, restaurant_repo
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

# ── LOGIN RATE LIMITER (in-process, resets on restart) ────────────────
_login_attempts: dict = defaultdict(list)
_LOGIN_MAX    = 10   # max attempts
_LOGIN_WINDOW = 900  # 15 minutes in seconds


def _check_login_rate_limit(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
    _login_attempts[ip].append(now)


# ── Pydantic models ──────────────────────────────────────────────────
class LoginRequest(BaseModel): username: str; password: str
class AdminLoginRequest(BaseModel): key: str
class CreateUserRequest(BaseModel): username: str; password: str; restaurant_id: int; admin_key: str = ""
class CreateRestaurantRequest(BaseModel): admin_key: str = ""; name: str; whatsapp_number: str; address: str; menu: str; features: dict = {}; wa_phone_id: str = ""; wa_access_token: str = ""
class SetSubscriptionRequest(BaseModel): admin_key: str = ""; restaurant_id: int; status: str
class UpdateRestaurantRequest(BaseModel):
    admin_key: str = ""; restaurant_id: int
    name: str = None; address: str = None; whatsapp_number: str = None
    wa_phone_id: str = None; wa_access_token: str = None
    features: dict = None; menu: str = None


# ── AUTH ──────────────────────────────────────────────────────────────

_ADMIN_ROLES = {"owner", "admin", "gerente"}
_ROLE_REDIRECT = {
    "mesero":       "/mesero",   "waiter":   "/mesero",
    "cocina":       "/cocina",   "cook":     "/cocina",   "cocinero": "/cocina",
    "caja":         "/caja",     "cashier":  "/caja",     "cajero":   "/caja",
    "bar":          "/bar",
    "domiciliario": "/domiciliario", "delivery": "/domiciliario",
}


@router.post("/api/auth/login")
async def auth_login(request: Request, body: LoginRequest):
    ip = request.client.host if request.client else "unknown"
    _check_login_rate_limit(ip)
    result = await login(body.username, body.password)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    await logout(token)
    return {"success": True}


@router.get("/api/auth/verify-role")
async def verify_role_for_page(request: Request, page: str):
    _PAGE_ROLES = {
        "mesero":       {"mesero", "waiter"},
        "caja":         {"caja", "cashier", "cajero"},
        "domiciliario": {"domiciliario", "delivery"},
        "cocina":       {"cocina", "cook", "cocinero"},
        "bar":          {"bar"},
        "staff-hq":     {"mesero", "waiter", "cocina", "cook", "cocinero", "caja", "cashier", "cajero", "bar", "domiciliario", "delivery", "otro"},
        "dashboard":    _ADMIN_ROLES,
        "settings":     _ADMIN_ROLES,
        "billing":      _ADMIN_ROLES,
    }

    try:
        user = await get_current_user(request)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    user_roles = {r.strip().lower() for r in (user.get("role") or "").split(",") if r.strip()}

    log.info("auth.verify_role", user_id=user.get("id"), page=page, roles=list(user_roles))

    if user_roles & _ADMIN_ROLES:
        return {"ok": True}

    allowed = _PAGE_ROLES.get(page, set())
    if not (user_roles & allowed):
        redirect_to = "/staff-hq"
        for role in user_roles:
            if role in _ROLE_REDIRECT:
                redirect_to = _ROLE_REDIRECT[role]
                break
        raise HTTPException(status_code=403, detail={"redirect": redirect_to})

    return {"ok": True}


# ── SUPER ADMIN SESSION ──────────────────────────────────────────────

@router.post("/api/admin/login")
async def admin_login(payload: AdminLoginRequest):
    """Exchange ADMIN_KEY for a session token. The raw key is never stored client-side."""
    if not payload.key or payload.key != os.getenv("ADMIN_KEY"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    token = await sessions_repo.create_session("superadmin")
    return {"token": token}


@router.post("/api/admin/logout")
async def admin_logout_session(req: Request):
    """Invalidate the current superadmin session token."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if token:
        await sessions_repo.delete_session(token)
    return {"success": True}


@router.get("/api/admin/stats")
async def admin_get_stats(_: None = Depends(verify_superadmin)):
    return await restaurant_repo.db_get_admin_stats()


@router.get("/api/admin/restaurants")
async def admin_get_restaurants(_: None = Depends(verify_superadmin)):
    return {"restaurants": await db.db_get_all_restaurants()}


@router.post("/api/admin/create-user")
async def admin_create_user(request: CreateUserRequest, _: None = Depends(verify_superadmin)):
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    success = await db.db_create_user(
        username=request.username,
        password_hash=hash_password(request.password),
        restaurant_name=rest["name"],
        role="owner",
        branch_id=request.restaurant_id
    )

    if not success:
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    return {"success": True}


@router.post("/api/admin/delete-user")
async def admin_delete_user(username: str, _: None = Depends(verify_superadmin)):
    await restaurant_repo.db_delete_user(username)
    return {"success": True}


@router.get("/api/admin/users")
async def admin_list_users(_: None = Depends(verify_superadmin)):
    return {"users": await get_users()}


@router.post("/api/admin/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest, _: None = Depends(verify_superadmin)):
    from app.routes.dashboard import geocode_address
    try:
        menu_dict = json.loads(request.menu)
    except Exception:
        raise HTTPException(status_code=400, detail="Menú no es JSON válido")
    lat, lon, _ = await geocode_address(request.address)

    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict, lat, lon, request.features)

    if request.wa_access_token:
        await restaurant_repo.db_set_restaurant_wa_credentials(
            request.whatsapp_number, request.wa_phone_id, request.wa_access_token
        )

    return {"success": True}


@router.post("/api/admin/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest, _: None = Depends(verify_superadmin)):
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True}


@router.get("/api/admin/restaurant/{restaurant_id}")
async def admin_get_restaurant_detail(restaurant_id: int, _: None = Depends(verify_superadmin)):
    rest = await db.db_get_restaurant_by_id(restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    wa = rest.get("whatsapp_number", "")
    stats = await restaurant_repo.db_get_restaurant_detail_stats(restaurant_id, wa)
    return {"restaurant": rest, "stats": stats}


@router.post("/api/admin/update-restaurant")
async def admin_update_restaurant(request: UpdateRestaurantRequest, _: None = Depends(verify_superadmin)):
    from app.routes.dashboard import geocode_address
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    lat = lon = None
    if request.address is not None:
        lat, lon, _ = await geocode_address(request.address)

    merged_features = None
    if request.features is not None:
        raw = rest.get("features") or {}
        current = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except Exception:
                current = {}
        current.update(request.features)
        merged_features = current

    parsed_menu = None
    if request.menu is not None:
        try:
            parsed_menu = json.loads(request.menu)
        except Exception:
            raise HTTPException(status_code=400, detail="Menú no es JSON válido")

    await restaurant_repo.db_update_restaurant_fields(
        request.restaurant_id,
        name=request.name,
        address=request.address,
        latitude=lat,
        longitude=lon,
        whatsapp_number=request.whatsapp_number,
        wa_phone_id=request.wa_phone_id,
        wa_access_token=request.wa_access_token,
        features=merged_features,
        menu=parsed_menu,
    )
    return {"success": True, "restaurant": await db.db_get_restaurant_by_id(request.restaurant_id)}


@router.get("/api/admin/billing-stats")
async def admin_billing_stats(_: None = Depends(verify_superadmin)):
    stats = await restaurant_repo.db_get_billing_stats()
    return {"stats": stats}


@router.post("/api/admin/fix-branch-ids")
async def fix_branch_ids(request: Request, _: None = Depends(verify_superadmin)):
    fixed = await restaurant_repo.db_fix_branch_ids()
    return {"success": True, "fixed": fixed}


@router.post("/api/admin/fix-conversations")
async def fix_conversations_bot_number(request: Request, _: None = Depends(verify_superadmin)):
    body = await request.json()
    await restaurant_repo.db_fix_conversations_bot_number(body.get("bot_number", ""))
    return {"success": True}


@router.post("/api/admin/parse-menu")
async def admin_parse_menu(file: UploadFile = File(...), _: None = Depends(verify_superadmin)):
    import pypdf
    content  = await file.read()
    filename = file.filename.lower()
    client   = Anthropic()
    messages_content = []
    try:
        if filename.endswith(".pdf"):
            pdf_reader = pypdf.PdfReader(io.BytesIO(content))
            text = "".join(p.extract_text() + "\n" for p in pdf_reader.pages)
            messages_content.append({"type": "text", "text": f"Extrae el menú:\n{text}"})
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            mt = "image/png" if filename.endswith(".png") else "image/jpeg"
            messages_content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(content).decode()}})
            messages_content.append({"type": "text", "text": "Extrae el menú de esta imagen."})
        else:
            raise HTTPException(status_code=400, detail="Sube PDF, PNG o JPG")
        response = client.messages.create(
            model="claude-3-haiku-20240307", max_tokens=4000, temperature=0,
            system='Extrae menús a JSON puro: {"Categoría": [{"name":"","price":0.0,"description":""}]}',
            messages=[{"role": "user", "content": messages_content}]
        )
        return {"success": True, "json_menu": json.loads(response.content[0].text.replace("```json","").replace("```","").strip())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
