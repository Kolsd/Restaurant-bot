"""
Auth routes: login/logout and role verification for restaurant users.

The superadmin CRUD endpoints (/api/admin/*) have been moved to
app/routes/internal/admin.py under /api/internal/admin/*.
"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.auth import login, logout, hash_password
from app.services import state_store
from app.services import realtime
from app.routes.deps import get_current_user
from app.services.logging import get_logger
from app.services.staff_sections import sections_for_roles
from app.services import ops_config, plan_access, plans
from app.services.tenant_context import tenant_scope
from app.routes.staff_ops import can_configure, resolve_ops_sede

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
# Every staff role lands on the unified /staff app now (Staff App unification,
# 2026-09-14) — the old one-HTML-per-role pages (/waiter, /cashier, /kitchen,
# /bar, /courier, /staff-hq) are removed. Which sections a role sees inside
# /staff is decided by app.services.staff_sections (single source of truth).
_ROLE_REDIRECT = {
    "mesero":       "/staff",   "waiter":   "/staff",
    "cocina":       "/staff",   "cook":     "/staff",  "cocinero": "/staff",
    "caja":         "/staff",   "cashier":  "/staff",  "cajero":   "/staff",
    "bar":          "/staff",
    "domiciliario": "/staff",   "delivery": "/staff",
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
    """Send a 6-digit OTP to the restaurant owner's email.

    Email is the only delivery channel (WhatsApp retired 2026-09-25).

    ALWAYS returns HTTP 200 with the SAME generic
    { "sent": true, "channel": "email" } response for any well-formed,
    non-rate-limited request — regardless of whether the email belongs to a
    real account, whether the lookup/token-creation succeeded, or whether
    the email actually got delivered. This is intentional: the frontend
    (app/static/js/pages/reset-password.js) already ignores these values and
    always shows a generic "si el correo existe, te enviamos un código"
    message, so returning anything that varies with account existence would
    only create an enumeration oracle with no upside. Failures are logged
    server-side (email-prefix only, never the full address or the code) so
    support can investigate. Only the rate-limit branch differs, and it does
    not leak existence either — it's keyed on the submitted string, not on
    whether that string maps to a real user.

    Rate-limited to 3 requests per email per 15 minutes.
    """
    from app.services import database as db  # noqa: PLC0415
    from app.repositories.password_reset_repo import db_create_password_reset  # noqa: PLC0415
    from app.services.email import send_email  # noqa: PLC0415
    from app.services.email_templates import render_password_reset_email  # noqa: PLC0415

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

    # Everything below is best-effort. It MUST NOT change the response value —
    # a lookup failure, an unknown email, a token-creation error, or a send
    # failure all look identical to the caller (anti-enumeration + the
    # frontend never branches on this anyway).
    generic_response = {"sent": True, "channel": "email"}

    # Look up user by email (username = email in the users table).
    try:
        user = await db.db_get_user(email)
    except Exception:
        log.exception("password_reset.user_lookup_error")
        return generic_response

    if not user:
        log.info("password_reset.user_not_found", email_prefix=email[:3] + "***")
        return generic_response

    restaurant_name: str = user.get("restaurant_name") or "tu restaurante"

    # Generate and store the OTP (unchanged: OTP_PEPPER + SHA-256 hashing
    # lives entirely in password_reset_repo.db_create_password_reset).
    try:
        code = await db_create_password_reset(email)
    except Exception:
        log.exception("password_reset.create_token_error", email_prefix=email[:3] + "***")
        return generic_response

    subject, html, text = render_password_reset_email(code=code, restaurant_name=restaurant_name)

    # Send via email (does not raise — returns bool).
    try:
        sent = await send_email(to=email, subject=subject, html=html, text=text)
    except Exception:
        log.exception("password_reset.email_send_error", email_prefix=email[:3] + "***")
        sent = False

    if not sent:
        # The OTP is safely stored regardless — the user can retry, or
        # support can look it up. Log server-side only; the response to the
        # caller stays generic (see docstring).
        log.warning("password_reset.email_send_failed", email_prefix=email[:3] + "***")

    return generic_response


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
        redirect_to = "/staff"
        for role in user_roles:
            if role in _ROLE_REDIRECT:
                redirect_to = _ROLE_REDIRECT[role]
                break
        raise HTTPException(status_code=403, detail={"redirect": redirect_to})

    return {"ok": True}


@router.get("/api/staff/sections")
async def staff_visible_sections(request: Request):
    """Canonical role -> Staff App section mapping for the current session.

    Backs the unified `/staff` shell (app/static/js/staff/staff-shell.js):
    the sidebar renders only the sections this endpoint returns. This is the
    ONE place that decides section visibility server-side — see
    app/services/staff_sections.py for the actual role -> section table.

    401 when the Bearer token is missing/invalid — this is the real,
    server-enforced "you need a session" gate for the Staff App (the HTML
    shell itself is served unconditionally, same as every operational page
    before it — the token never reaches a plain browser navigation, only
    fetch() calls made after the page's own JS runs).
    """
    user = await get_current_user(request)
    roles = [r.strip() for r in (user.get("role") or "").split(",") if r.strip()]
    sections = sections_for_roles(roles)

    # What the sede said it uses (migration 0105, app/services/ops_config.py)
    # and what the plan unlocks narrow the role's sections further.
    needs_setup = False
    loc = None
    configurable = can_configure(user)
    org_id = int(user.get("org_id") or 0)
    if org_id and sections:
        with tenant_scope(org_id):
            if not await plan_access.org_has_feature(org_id, plans.DELIVERY):
                sections = [s for s in sections if s not in ("delivery", "courier")]
            loc = await resolve_ops_sede(request, user, org_id)
            if loc:
                cfg = await ops_config.get_for_sede(org_id, loc)
                sections = ops_config.visible_sections(sections, cfg)
                needs_setup = configurable and not cfg["configured"]
    configurable = configurable and bool(loc)

    return {
        "ok": True, "roles": roles, "sections": sections,
        "needs_setup": needs_setup, "can_configure": configurable,
    }


@router.get("/api/staff/stream")
async def staff_stream(request: Request):
    """Real-time SSE feed for the Staff App (kitchen/bar/waiter/cashier/courier).

    Same auth gate as GET /api/staff/sections (Bearer token — admin sessions
    and staff PIN sessions), 401 without it. Streams every invalidation event
    (see app/services/realtime.py) for the caller's org — no location
    filtering, sections filter client-side by topic (mesio-realtime.js).

    Auth is resolved once, here, before the generator starts; the generator
    itself never touches the DB (CLAUDE.md "4 workers" — no long-held
    connection while streaming).
    """
    user = await get_current_user(request)
    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    org_id = int(org_id)

    return StreamingResponse(
        realtime.event_stream(request.is_disconnected, org_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
