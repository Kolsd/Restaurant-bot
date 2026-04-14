"""
Auth routes: login/logout and role verification for restaurant users.

The superadmin CRUD endpoints (/api/admin/*) have been moved to
app/routes/internal/admin.py under /api/internal/admin/*.
"""
import time
from collections import defaultdict
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.services.auth import login, logout
from app.routes.deps import get_current_user
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
