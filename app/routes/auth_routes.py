"""
Auth routes: login/logout and role verification for restaurant users.

The superadmin CRUD endpoints (/api/admin/*) have been moved to
app/routes/internal/admin.py under /api/internal/admin/*.
"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from app.services.auth import login, logout, hash_password
from app.services import state_store
from app.routes.deps import get_current_user
from app.services.logging import get_logger

log = get_logger(__name__)

_FORGOT_PW_MAX    = 3    # max reset requests per email per window
_FORGOT_PW_WINDOW = 900  # 15 minutes in seconds

router = APIRouter()

_LOGIN_MAX    = 10   # max attempts per IP per window
_LOGIN_WINDOW = 900  # 15 minutes in seconds


async def _check_login_rate_limit(ip: str) -> None:
    """Rate-limit login attempts via Redis (cross-worker safe)."""
    key = f"login_attempts:{ip}"
    allowed = await state_store.rate_limit_check(
        key=key, max_requests=_LOGIN_MAX, window_seconds=_LOGIN_WINDOW
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")


# ── Pydantic models ──────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=254)
    password: str = Field(..., min_length=1, max_length=1024)


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
    await _check_login_rate_limit(ip)
    result = await login(body.username, body.password)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    await logout(token)
    return {"success": True}


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


@router.post("/api/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    """Send a 6-digit OTP to the restaurant owner's WhatsApp.

    ALWAYS returns HTTP 200 with { "sent": bool, "channel": "whatsapp" | null }
    to prevent attacker enumeration of valid email addresses. Rate-limited to
    3 requests per email per 15 minutes.
    """
    from app.services import database as db  # noqa: PLC0415
    from app.repositories.password_reset_repo import db_create_password_reset  # noqa: PLC0415
    from app.services.whatsapp_messaging import send_password_reset_code  # noqa: PLC0415

    email = (body.email or "").lower().strip()
    if not email:
        return {"sent": False, "channel": None}

    # Rate limit: 3 attempts per email per 15 min (Redis cross-worker safe).
    rate_key = f"forgot_pw:{email}"
    allowed = await state_store.rate_limit_check(
        key=rate_key, max_requests=_FORGOT_PW_MAX, window_seconds=_FORGOT_PW_WINDOW
    )
    if not allowed:
        log.warning("password_reset.rate_limited", email_prefix=email[:3] + "***")
        return {"sent": False, "channel": None}

    # Look up user by email (username = email in the users table).
    try:
        user = await db.db_get_user(email)
    except Exception:
        log.exception("password_reset.user_lookup_error")
        return {"sent": False, "channel": None}

    if not user:
        # Anti-enumeration: return the same shape as success.
        log.info("password_reset.user_not_found", email_prefix=email[:3] + "***")
        return {"sent": False, "channel": None}

    # Resolve the org's WhatsApp number (the owner's contact line).
    whatsapp_number: str | None = None
    restaurant_name: str = user.get("restaurant_name", "")
    branch_id = user.get("branch_id")

    try:
        if branch_id:
            restaurant = await db.db_get_restaurant_by_id(branch_id)
            if restaurant:
                whatsapp_number = restaurant.get("whatsapp_number") or None
                restaurant_name = restaurant.get("name") or restaurant_name
        else:
            # No branch_id: attempt org lookup by restaurant_name.
            target_name = restaurant_name.lower().strip()
            if target_name:
                all_orgs = await db.db_get_all_orgs(active_only=False)
                for org in all_orgs:
                    if (org.get("name") or "").lower().strip() == target_name:
                        whatsapp_number = org.get("whatsapp_number") or None
                        restaurant_name = org.get("name") or restaurant_name
                        break
    except Exception:
        log.exception("password_reset.org_lookup_error", email_prefix=email[:3] + "***")
        return {"sent": False, "channel": None}

    if not whatsapp_number:
        log.warning(
            "password_reset.no_whatsapp_number",
            email_prefix=email[:3] + "***",
            restaurant_name=restaurant_name,
        )
        return {"sent": False, "channel": None}

    # Generate and store the OTP.
    try:
        code = await db_create_password_reset(email)
    except Exception:
        log.exception("password_reset.create_token_error", email_prefix=email[:3] + "***")
        return {"sent": False, "channel": None}

    # Send via WhatsApp (does not raise — returns bool).
    sent = await send_password_reset_code(
        whatsapp_number=whatsapp_number,
        code=code,
        restaurant_name=restaurant_name,
    )

    return {"sent": sent, "channel": "whatsapp" if sent else None}


@router.post("/api/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    """Verify the OTP and set a new password.

    Returns:
        200 { "ok": true }               — success (all sessions revoked)
        400 { "error": "weak_password" } — password < 8 chars
        400 { "error": "invalid_code" }  — wrong code or no active token
        400 { "error": "expired" }       — token TTL passed
        400 { "error": "too_many_attempts" } — 5+ wrong codes
    """
    from app.services import database as db  # noqa: PLC0415
    from app.repositories.password_reset_repo import db_consume_password_reset  # noqa: PLC0415
    from app.repositories.sessions_repo import delete_sessions_for_user  # noqa: PLC0415

    email = (body.email or "").lower().strip()
    code = (body.code or "").strip()
    new_password = body.new_password or ""

    # Validate new password length before hitting the DB.
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail={"error": "weak_password"})

    if not email or not code:
        raise HTTPException(status_code=400, detail={"error": "invalid_code"})

    # Verify and consume the OTP.
    try:
        ok, error_code = await db_consume_password_reset(email, code)
    except Exception:
        log.exception("password_reset.consume_error", email_prefix=email[:3] + "***")
        raise HTTPException(status_code=400, detail={"error": "invalid_code"})

    if not ok:
        raise HTTPException(status_code=400, detail={"error": error_code})

    # Hash the new password with bcrypt.
    new_hash = hash_password(new_password)

    # Persist the new password hash.
    try:
        updated = await db.db_update_user_password(email, new_hash)
    except Exception:
        log.exception("password_reset.update_password_error", email_prefix=email[:3] + "***")
        raise HTTPException(status_code=400, detail={"error": "invalid_code"})

    if not updated:
        # User vanished between OTP creation and now — treat as failure.
        log.error("password_reset.user_disappeared", email_prefix=email[:3] + "***")
        raise HTTPException(status_code=400, detail={"error": "invalid_code"})

    # Invalidate ALL existing sessions (force re-login on all devices).
    try:
        revoked = await delete_sessions_for_user(email)
        log.info(
            "password_reset.sessions_revoked",
            count=revoked,
            email_prefix=email[:3] + "***",
        )
    except Exception:
        # Non-fatal: password was changed successfully. Old sessions will
        # expire naturally. Log for observability.
        log.exception("password_reset.session_revoke_error", email_prefix=email[:3] + "***")

    log.info("password_reset.success", email_prefix=email[:3] + "***")
    return {"ok": True}


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
