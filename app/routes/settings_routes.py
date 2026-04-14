"""
Settings routes: restaurant settings (GET/POST) and all /api/dashboard/* data endpoints.
Also includes the order-status update and table-session helpers that power the dashboard UI.
"""
import json
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Request, HTTPException, Depends
from anthropic import Anthropic

from app.services import database as db
from app.routes.deps import require_auth, get_current_user, get_current_restaurant
from app.repositories import restaurant_repo, tables_repo as tr
from app.repositories import weekly_reports_repo
from app.repositories.staff_repo import db_has_staff
from app.repositories.restaurant_repo import db_has_orders_by_bot_number
from app.services.logging import get_logger
from app.services import state_store
from pydantic import BaseModel

log = get_logger(__name__)

router = APIRouter()


# ── SETTINGS ─────────────────────────────────────────────────────────

@router.get("/api/settings")
async def get_settings(request: Request):
    user = await get_current_user(request)
    branch_id = user.get("branch_id")
    branch_header = request.headers.get("X-Branch-ID")

    if branch_header and branch_header.isdigit() and user.get("role", "") in ("owner", "admin"):
        branch_id = int(branch_header)

    restaurant = await db.db_get_restaurant_by_id(branch_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    raw_features = restaurant.get("features", {}) or {}
    if isinstance(raw_features, str):
        try:
            features = json.loads(raw_features)
        except Exception:
            features = {}
    else:
        features = raw_features

    return {
        "restaurant_id": restaurant["id"],
        "name": restaurant["name"],
        "whatsapp_number": restaurant.get("whatsapp_number", ""),
        "address": restaurant.get("address", ""),
        "features": features,
        "payment_methods": features.get("payment_methods", []),
        "payment_instructions": features.get("payment_instructions", {}),
        "google_maps_url": features.get("google_maps_url", ""),
        "bot_active": features.get("bot_active", True),
        "upsell_active": features.get("upsell_active", True),
        "domicilio_active": features.get("domicilio_active", True),
        "recoger_active": features.get("recoger_active", True),
        "delivery_fee": features.get("delivery_fee", 0),
        "min_order": features.get("min_order", 0),
        "delivery_radius_km": features.get("delivery_radius_km", 5),
        "timezone": features.get("timezone", "America/Bogota"),
        "currency": features.get("currency", "COP"),
        "locale": features.get("locale", "es-CO"),
        "latitude": restaurant.get("latitude"),
        "longitude": restaurant.get("longitude"),
        # Catálogo visual v2 — Fase 1
        "bot_visual_menu": features.get("bot_visual_menu", False),
        "catalog_v2_enabled": features.get("catalog_v2_enabled", True),
    }


@router.post("/api/settings")
async def save_settings(request: Request):
    user = await get_current_user(request)

    branch_id = user.get("branch_id")
    branch_header = request.headers.get("X-Branch-ID")

    if branch_header == "all":
        raise HTTPException(status_code=400, detail="No puedes editar configuración en modo 'Todas las sucursales'. Selecciona una específica.")

    if branch_header and branch_header.isdigit() and user.get("role", "") in ("owner", "admin"):
        branch_id = int(branch_header)

    restaurant = await db.db_get_restaurant_by_id(branch_id)
    body = await request.json()

    raw_features = restaurant.get("features", {}) or {}
    current_features = json.loads(raw_features) if isinstance(raw_features, str) else dict(raw_features)

    updatable = [
        "payment_methods", "payment_instructions", "google_maps_url", "bot_active",
        "upsell_active", "domicilio_active", "recoger_active",
        "delivery_fee", "min_order", "delivery_radius_km", "delivery_message",
        "pickup_message", "welcome_message",
        "timezone", "currency", "locale",
        # Catálogo visual v2 — Fase 1
        "bot_visual_menu", "catalog_v2_enabled",
    ]
    for key in updatable:
        if key in body:
            current_features[key] = body[key]

    lat = float(body["latitude"]) if "latitude" in body and body["latitude"] not in [None, ""] else None
    lon = float(body["longitude"]) if "longitude" in body and body["longitude"] not in [None, ""] else None
    await restaurant_repo.db_save_restaurant_settings(restaurant["id"], current_features, latitude=lat, longitude=lon)
    return {"success": True, "features": current_features}


# ── WEEKLY REPORTS ────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


class WeeklyReportSettings(BaseModel):
    enabled: bool | None = None
    owner_phone: str | None = None
    timezone: str | None = None


@router.get("/api/weekly-reports")
async def get_weekly_reports(
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return the last 12 weekly reports and current settings for the authenticated restaurant."""
    restaurant_id = restaurant["id"]

    reports_raw = await weekly_reports_repo.get_recent_reports(restaurant_id, limit=12)

    reports = []
    for r in reports_raw:
        reports.append({
            "id": r["id"],
            "week_start": r["week_start"].isoformat() if hasattr(r.get("week_start"), "isoformat") else str(r.get("week_start", "")),
            "generated_at": r["generated_at"].isoformat() + "Z" if r.get("generated_at") else None,
            "sent_at": r["sent_at"].isoformat() + "Z" if r.get("sent_at") else None,
            "delivery_status": r.get("delivery_status"),
            "error_message": r.get("error_message"),
            "payload": r.get("payload"),
        })

    raw_features = restaurant.get("features") or {}
    if isinstance(raw_features, str):
        try:
            features = json.loads(raw_features)
        except Exception:
            features = {}
    else:
        features = dict(raw_features)

    settings = {
        "enabled": features.get("weekly_report_enabled", True),
        "owner_phone": restaurant.get("owner_phone"),
        "timezone": restaurant.get("timezone") or "America/Bogota",
    }

    return {"reports": reports, "settings": settings}


@router.patch("/api/weekly-reports/settings")
async def patch_weekly_report_settings(
    body: WeeklyReportSettings,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Update weekly report settings (enabled flag, owner_phone, timezone)."""
    restaurant_id = restaurant["id"]

    # ── Validate owner_phone ──────────────────────────────────────────
    if body.owner_phone is not None:
        phone = body.owner_phone.strip()
        if phone == "":
            # Empty string clears the phone
            phone = None
        else:
            # Normalize: leading "00" → "+"
            if phone.startswith("00"):
                phone = "+" + phone[2:]
            if not _PHONE_RE.match(phone):
                raise HTTPException(
                    status_code=422,
                    detail="owner_phone debe seguir el formato E.164 (ej. +573001234567, 10–15 dígitos)",
                )
        await restaurant_repo.db_update_restaurant_owner_phone(restaurant_id, phone)
        log.info("weekly_reports.settings.phone_updated", restaurant_id=restaurant_id)

    # ── Validate timezone ─────────────────────────────────────────────
    if body.timezone is not None:
        tz_str = body.timezone.strip()
        if tz_str == "":
            raise HTTPException(
                status_code=422,
                detail="timezone no puede ser vacío. Usa un nombre IANA válido (ej. America/Bogota)",
            )
        try:
            ZoneInfo(tz_str)
        except ZoneInfoNotFoundError:
            raise HTTPException(
                status_code=422,
                detail=f"timezone inválido: '{tz_str}'. Usa un nombre IANA válido (ej. America/Bogota, America/New_York)",
            )
        await restaurant_repo.db_update_restaurant_timezone(restaurant_id, tz_str)
        log.info("weekly_reports.settings.timezone_updated", restaurant_id=restaurant_id, timezone=tz_str)

    # ── Update enabled flag in features JSONB ────────────────────────
    if body.enabled is not None:
        await restaurant_repo.db_merge_restaurant_features(
            restaurant_id, {"weekly_report_enabled": body.enabled}
        )
        log.info(
            "weekly_reports.settings.enabled_updated",
            restaurant_id=restaurant_id,
            enabled=body.enabled,
        )

    # ── Re-fetch to return authoritative state ────────────────────────
    updated = await db.db_get_restaurant_by_id(restaurant_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    raw_features = updated.get("features") or {}
    if isinstance(raw_features, str):
        try:
            features = json.loads(raw_features)
        except Exception:
            features = {}
    else:
        features = dict(raw_features)

    return {
        "enabled": features.get("weekly_report_enabled", True),
        "owner_phone": updated.get("owner_phone"),
        "timezone": updated.get("timezone") or "America/Bogota",
    }


# ── ONBOARDING STATUS ────────────────────────────────────────────────

@router.get("/api/onboarding/status")
async def get_onboarding_status(request: Request):
    """
    Returns the onboarding completion status for the currently authenticated restaurant.
    Each criterion is checked independently; failures default to done=False.
    Score = number of completed steps × 20 (5 steps × 20 = 100 max).
    """
    user = await get_current_user(request)
    branch_id = user.get("branch_id")
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit() and user.get("role", "") in ("owner", "admin"):
        branch_id = int(branch_header)

    restaurant = await db.db_get_restaurant_by_id(branch_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    restaurant_id = restaurant["id"]
    whatsapp_number = restaurant.get("whatsapp_number") or ""

    raw_features = restaurant.get("features") or {}
    if isinstance(raw_features, str):
        try:
            features = json.loads(raw_features)
        except Exception:
            features = {}
    else:
        features = dict(raw_features)

    # ── 1. has_menu ───────────────────────────────────────────────────
    has_menu = False
    try:
        raw_menu = restaurant.get("menu")
        if raw_menu:
            if isinstance(raw_menu, str):
                menu = json.loads(raw_menu)
            else:
                menu = raw_menu
            if isinstance(menu, dict):
                has_menu = any(
                    isinstance(v, list) and len(v) > 0
                    for v in menu.values()
                )
            elif isinstance(menu, list):
                has_menu = len(menu) > 0
    except Exception as exc:
        log.warning("onboarding.menu_check_failed", error=str(exc))

    # If the restaurant dict doesn't carry menu directly, fall back to db_get_menu
    if not has_menu and whatsapp_number:
        try:
            menu = await db.db_get_menu(whatsapp_number) or {}
            if isinstance(menu, dict):
                has_menu = any(
                    isinstance(v, list) and len(v) > 0
                    for v in menu.values()
                )
            elif isinstance(menu, list):
                has_menu = len(menu) > 0
        except Exception as exc:
            log.warning("onboarding.menu_fallback_check_failed", error=str(exc))

    # ── 2. has_staff ──────────────────────────────────────────────────
    has_staff = False
    try:
        has_staff = await db_has_staff(restaurant_id)
    except Exception as exc:
        log.warning("onboarding.staff_check_failed", error=str(exc))

    # ── 3. has_billing ────────────────────────────────────────────────
    has_billing = bool(
        features.get("billing_provider")
        or features.get("alegra_email")
        or features.get("billing_enabled")
    )

    # ── 4. has_whatsapp ───────────────────────────────────────────────
    has_whatsapp = bool(whatsapp_number.strip())

    # ── 5. has_first_order ───────────────────────────────────────────
    has_first_order = False
    if whatsapp_number:
        try:
            has_first_order = await db_has_orders_by_bot_number(whatsapp_number)
        except Exception as exc:
            log.warning("onboarding.first_order_check_failed", error=str(exc))

    steps = {
        "menu": {
            "done": has_menu,
            "label": "Carta del restaurante",
            "description": "Sube tu menú para que los clientes puedan pedir por WhatsApp",
        },
        "staff": {
            "done": has_staff,
            "label": "Equipo operativo",
            "description": "Agrega al menos un empleado para gestionar turnos y nómina",
        },
        "billing": {
            "done": has_billing,
            "label": "Facturación electrónica",
            "description": "Configura tu proveedor de facturación DIAN",
        },
        "whatsapp": {
            "done": has_whatsapp,
            "label": "WhatsApp conectado",
            "description": "Conecta tu número de WhatsApp Business",
        },
        "first_order": {
            "done": has_first_order,
            "label": "Primer pedido",
            "description": "Recibe tu primer pedido a través del bot",
        },
    }

    score = sum(20 for s in steps.values() if s["done"])
    return {"score": score, "steps": steps}


# ── SHARED FILTER HELPER ─────────────────────────────────────────────

async def get_dashboard_filters(request: Request, period: str, custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")

    branch_id = user.get("branch_id")
    branch_header = request.headers.get("X-Branch-ID")

    if branch_header == "all" and user.get("role", "") in ("owner", "admin"):
        branch_id = "all"
    elif branch_header and branch_header.isdigit() and user.get("role", "") in ("owner", "admin"):
        branch_id = int(branch_header)

    bot_number = None
    if branch_id and branch_id != "all":
        r = await db.db_get_restaurant_by_id(branch_id)
        if r:
            bot_number = r.get("whatsapp_number")
    elif branch_id == "all":
        r = await db.db_get_restaurant_by_id(user.get("branch_id"))
        if r:
            bot_number = r.get("whatsapp_number")

    now_utc = datetime.utcnow()
    now_local = now_utc - timedelta(minutes=tz_offset)
    end_local = now_local + timedelta(days=1)
    end_local = end_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "custom" and custom_start and custom_end:
        start_local = datetime.strptime(custom_start, "%Y-%m-%d")
        end_local = datetime.strptime(custom_end, "%Y-%m-%d") + timedelta(days=1)
    elif period == "week":
        start_local = now_local - timedelta(days=7)
    elif period == "month":
        start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "semester":
        start_local = now_local - timedelta(days=180)
    elif period == "year":
        start_local = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    start_date = start_local + timedelta(minutes=tz_offset)
    end_date = end_local + timedelta(minutes=tz_offset)

    return branch_id, bot_number, start_date, end_date


# ── DASHBOARD DATA ENDPOINTS ─────────────────────────────────────────

@router.get("/api/dashboard/orders")
async def get_dashboard_orders(request: Request, period: str = "today", custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    branch_id, bot_number, start_date, end_date = await get_dashboard_filters(request, period, custom_start, custom_end, tz_offset)

    orders = []
    try:
        rows_wa, rows_mesa = await restaurant_repo.db_get_dashboard_orders(
            start_date, end_date, branch_id, bot_number
        )
        for r in rows_wa:
            orders.append({
                "id": r["id"],
                "items": r["items"],
                "type": r.get("order_type", "domicilio"),
                "status": r.get("status", "pendiente"),
                "paid": r.get("payment_status") == "paid" or r.get("paid") == True,
                "total": float(r["total"] or 0),
                "time": r["created_at"].strftime("%H:%M"),
                "created_at": r["created_at"].isoformat() + "Z",
                "address": r.get("address", ""),
                "payment_method": r.get("payment_method", ""),
                "notes": r.get("notes", ""),
                "phone": r.get("phone", ""),
            })
    except Exception as e:
        log.error("dashboard.stats_orders_load_failed", error=str(e))

    try:
        mesa_groups = {}
        for r in rows_mesa:
            if not r["created_at"]:
                continue
            base_id = r["base_order_id"] if r.get("base_order_id") else r["id"]
            if base_id not in mesa_groups:
                mesa_groups[base_id] = {
                    "id": base_id, "items": [], "status": r.get("status") or "recibido",
                    "total": 0.0, "is_paid": False,
                    "time": r["created_at"].strftime("%H:%M"),
                    "created_at": r["created_at"].isoformat() + "Z"
                }
            mesa_groups[base_id]["total"] += float(r["total"] or 0)

            try:
                raw_items = r["items"]
                if isinstance(raw_items, str):
                    parsed_items = json.loads(raw_items)
                elif isinstance(raw_items, list):
                    parsed_items = raw_items
                else:
                    parsed_items = []
                if isinstance(parsed_items, list):
                    mesa_groups[base_id]["items"].extend(parsed_items)
            except Exception:
                pass

            row_status = r.get("status") or ""
            if row_status in ["factura_generada", "factura_entregada", "cerrar_mesa"]:
                mesa_groups[base_id]["is_paid"] = True
                mesa_groups[base_id]["status"] = row_status

        for base_id, g in mesa_groups.items():
            orders.append({
                "id": g["id"], "items": json.dumps(g["items"], default=str), "type": "mesa",
                "status": g["status"], "paid": g["is_paid"], "total": g["total"],
                "time": g["time"], "created_at": g["created_at"]
            })
    except Exception as e:
        log.error("dashboard.stats_table_orders_load_failed", error=str(e))

    orders.sort(key=lambda x: x["created_at"], reverse=True)
    return {"orders": orders}


@router.post("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, request: Request):
    await require_auth(request)
    body = await request.json()
    new_status = body.get("status", "")
    if not new_status:
        raise HTTPException(status_code=400, detail="status requerido")
    await db.db_update_order_status(order_id, new_status)
    return {"success": True}


@router.get("/api/table-sessions/closed")
async def get_closed_sessions(request: Request, hours: int = 24):
    hours = max(1, min(hours, 720))  # clamp: 1h – 30 days
    _, bot_number, _, _ = await get_dashboard_filters(request, "today")

    try:
        rows = await tr.db_get_closed_sessions(hours, bot_number)
    except Exception as e:
        log.warning("dashboard.table_sessions_query_failed", error=str(e))
        rows = []

    sessions = []
    for r in rows:
        s = dict(r)
        if s.get("started_at"): s["started_at"] = s["started_at"].isoformat() + "Z"
        if s.get("closed_at"): s["closed_at"] = s["closed_at"].isoformat() + "Z"
        sessions.append(s)

    return {"sessions": sessions}


@router.get("/api/dashboard/reservations")
async def get_dashboard_reservations(request: Request, period: str = "today", custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    _, bot_number, start_date, end_date = await get_dashboard_filters(request, period, custom_start, custom_end, tz_offset)

    reservations = []
    try:
        rows = await restaurant_repo.db_get_dashboard_reservations(start_date, end_date, bot_number)
        for r in rows:
            reservations.append({
                "id": r["id"], "name": r["name"], "date": str(r["date"]),
                "time": str(r["time"])[:5], "guests": r["guests"],
                "phone": r["phone"], "notes": r["notes"]
            })
    except Exception:
        pass

    return {"reservations": reservations}


@router.get("/api/dashboard/conversations")
async def get_dashboard_conversations(request: Request):
    branch_id, bot_number, _, _ = await get_dashboard_filters(request, "today")

    if bot_number:
        bot_number = bot_number.split("_b")[0]

    rows = await restaurant_repo.db_get_dashboard_conversations(branch_id, bot_number)

    convs = []
    for r in rows:
        try:
            history = json.loads(r["history"]) if isinstance(r["history"], str) else r["history"]
            preview = history[-1]["content"] if history else "Conversación iniciada..."
            if isinstance(preview, dict):
                preview = "Multimedia/Sistema"
        except Exception:
            history = []
            preview = "Conversación activa..."

        has_voucher = any(
            "/api/media/" in (m.get("content") or "")
            for m in history if isinstance(m.get("content"), str)
        )

        convs.append({
            "phone": r["phone"],
            "messages": len(history),
            "preview": preview[:60] + "..." if len(preview) > 60 else preview,
            "last_updated": r["updated_at"].isoformat() + "Z",
            "has_voucher": has_voucher,
        })
    return {"conversations": convs}


@router.get("/api/dashboard/menu")
async def get_dashboard_menu(request: Request):
    _, bot_number, _, _ = await get_dashboard_filters(request, "today")
    menu = await db.db_get_menu(bot_number) or {}
    return {"menu": menu}


@router.get("/api/table-sessions/{session_id}/history")
async def get_session_history(request: Request, session_id: int):
    await require_auth(request)
    session, history = await tr.db_get_session_with_history(session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    if session.get("started_at"): session["started_at"] = session["started_at"].isoformat()
    if session.get("closed_at"): session["closed_at"] = session["closed_at"].isoformat()
    return {"session": session, "history": history}


@router.post("/api/table-sessions/{session_id}/reopen")
async def reopen_session(request: Request, session_id: int):
    await require_auth(request)
    await tr.db_reopen_session(session_id)
    return {"success": True}


@router.post("/api/table-sessions/{session_id}/alert-waiter")
async def session_alert_waiter(request: Request, session_id: int):
    body = await request.json()
    await tr.db_session_alert_waiter(session_id, body.get("message", "Alerta de dashboard"))
    return {"success": True}


# ── AI PROXY ─────────────────────────────────────────────────────────

class _AIProxyRequest(BaseModel):
    system: str
    user: str
    max_tokens: int = 1000


_ai_client: Anthropic | None = None


def _get_ai_client() -> Anthropic:
    global _ai_client
    if _ai_client is None:
        _ai_client = Anthropic()
    return _ai_client


@router.post("/api/ai/proxy")
async def ai_proxy(payload: _AIProxyRequest, _user: str = Depends(require_auth)):
    """
    Proxy autenticado para llamadas al modelo de IA desde el dashboard.
    El ANTHROPIC_API_KEY vive solo en el servidor — nunca se expone al cliente.
    Requiere Bearer token de admin válido.
    """
    max_tok = min(max(1, payload.max_tokens), 2000)  # clamp 1–2000
    try:
        client = _get_ai_client()
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tok,
            system=payload.system,
            messages=[{"role": "user", "content": payload.user}],
        )
        text = resp.content[0].text if resp.content else ""
        return {"text": text}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI service error: {exc}") from exc


# ── CATÁLOGO VISUAL v2 — Image endpoints ──────────────────────────────────────

class _ImageSignRequest(BaseModel):
    folder_suffix: Optional[str] = "menu"


class _ImageDeleteRequest(BaseModel):
    public_id: str


@router.post("/api/menu/image/sign")
async def sign_image_upload(
    body: _ImageSignRequest,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Retorna parámetros firmados para upload directo browser→Cloudinary.

    El browser hace POST multipart a:
        https://api.cloudinary.com/v1_1/{cloud_name}/image/upload
    usando estos params + el archivo elegido. Nuestro servidor nunca toca los bytes.

    Rate limit: 30 requests/min por restaurante (Redis cross-worker).
    Auth: Bearer token de admin/owner del restaurante.
    """
    from app.services import image_host

    restaurant_id = restaurant["id"]
    folder_suffix = (body.folder_suffix or "menu").strip() or "menu"

    # ── Rate limit: 30 uploads/min por restaurante ────────────────────────────
    rl_key = f"menu_image_sign:{restaurant_id}"
    allowed = await state_store.rate_limit_check(
        key=rl_key, max_requests=30, window_seconds=60
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Demasiadas solicitudes de upload. Espera un momento e intenta de nuevo.",
        )

    # ── Firma Cloudinary ──────────────────────────────────────────────────────
    result = image_host.sign_upload_params(restaurant_id, folder_suffix=folder_suffix)

    if "error" in result:
        log.warning(
            "menu.image.sign.config_missing",
            restaurant_id=restaurant_id,
            error=result["error"],
        )
        raise HTTPException(
            status_code=503,
            detail="Image hosting no configurado. Contacta soporte.",
        )

    log.info("menu.image.sign", restaurant_id=restaurant_id, folder=result.get("folder"))
    return result


@router.delete("/api/menu/image")
async def delete_menu_image(
    body: _ImageDeleteRequest,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Borra una imagen de Cloudinary por public_id.

    Valida ownership: el public_id DEBE comenzar con mesio/r_{restaurant_id}/.
    Si no pertenece al restaurante autenticado → 403.
    Si Cloudinary falla (imagen ya no existe, etc.) → 200 igual (idempotente).

    Auth: Bearer token de admin/owner del restaurante.
    """
    from app.services import image_host

    restaurant_id = restaurant["id"]
    public_id = (body.public_id or "").strip()

    if not public_id:
        raise HTTPException(status_code=422, detail="public_id es requerido.")

    # ── Ownership check: verificar antes de intentar borrar ───────────────────
    expected_prefix = f"mesio/r_{restaurant_id}/"
    if not public_id.startswith(expected_prefix):
        log.warning(
            "menu.image.delete.ownership_denied",
            restaurant_id=restaurant_id,
            public_id=public_id,
            expected_prefix=expected_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail="No puedes borrar esta imagen.",
        )

    # ── Borrar de Cloudinary (idempotente: si ya no existe, igual 200) ────────
    success = image_host.delete_image(public_id, restaurant_id)

    if not success:
        # delete_image retorna False tanto si ownership falla (ya verificado arriba)
        # como si Cloudinary falla o la imagen no existe. En ambos casos loguear
        # y retornar 200 para mantener idempotencia — el recurso ya no existe.
        log.warning(
            "menu.image.delete.cloudinary_noop",
            restaurant_id=restaurant_id,
            public_id=public_id,
        )
    else:
        log.info("menu.image.delete", restaurant_id=restaurant_id, public_id=public_id)

    return {"success": True, "public_id": public_id}


# ── Catalog v2: menu engineering analytics (Fase 5b) ─────────────────────────

@router.get("/api/menu/analytics")
async def menu_analytics(
    days: int = 30,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Return per-dish event counts and menu-engineering quadrant matrix.
    Auth: admin/owner Bearer token.
    Query param: days (default 30, max 365).
    """
    from app.repositories import menu_analytics_repo

    days = max(1, min(days, 365))
    restaurant_id = restaurant["id"]

    per_dish = await menu_analytics_repo.get_dish_analytics(restaurant_id, days)
    matrix   = await menu_analytics_repo.get_menu_engineering_matrix(restaurant_id, days)

    return {"per_dish": per_dish, "matrix": matrix}
