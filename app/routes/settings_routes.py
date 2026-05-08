"""
Settings routes: restaurant settings (GET/POST) and all /api/dashboard/* data endpoints.
Also includes the order-status update and table-session helpers that power the dashboard UI.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Request, HTTPException, Depends
from anthropic import Anthropic

from app.services import database as db
from app.routes.deps import require_auth, get_current_user, get_current_restaurant
from app.repositories import restaurant_repo, tables_repo as tr
from app.repositories import weekly_reports_repo
from app.repositories.staff_repo import db_has_staff
from app.services.tenant_context import bypass_tenant_scope, tenant_scope
from app.repositories.restaurant_repo import db_has_orders_by_bot_number
from app.services.logging import get_logger
from app.services import state_store
from pydantic import BaseModel

log = get_logger(__name__)

router = APIRouter()


# ── SETTINGS ─────────────────────────────────────────────────────────

def _parse_features(restaurant: dict) -> dict:
    """Normalise features field from a restaurant dict into a plain Python dict."""
    raw = restaurant.get("features", {}) or {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return dict(raw)


def _mask_wompi_for_response(features: dict) -> dict:
    """Return a sanitized copy of features.wompi for GET responses.

    The integrity_secret is a sensitive credential — NEVER returned in plaintext.
    The public_key is shown raw (it is intentionally public). The secret is
    replaced with a `set` boolean and a `last4` hint for the UI to display
    "•••• 7891"-style placeholders without revealing the value.
    """
    out = dict(features) if isinstance(features, dict) else {}
    raw_wompi = out.get("wompi") or {}
    if not isinstance(raw_wompi, dict):
        out["wompi"] = {"public_key": "", "integrity_secret_set": False, "integrity_secret_last4": ""}
        return out
    secret = (raw_wompi.get("integrity_secret") or "").strip()
    out["wompi"] = {
        "public_key":              (raw_wompi.get("public_key") or "").strip(),
        "integrity_secret_set":    bool(secret),
        "integrity_secret_last4":  secret[-4:] if len(secret) >= 4 else "",
    }
    return out


def _build_settings_response(restaurant: dict, features: dict) -> dict:
    """Build the canonical settings response payload."""
    safe_features = _mask_wompi_for_response(features)
    return {
        "restaurant_id":       restaurant["id"],
        "name":                restaurant.get("name", ""),
        "whatsapp_number":     restaurant.get("whatsapp_number", ""),
        "address":             restaurant.get("address", ""),
        # Fields persisted in features JSONB (no dedicated column)
        "nit":                 features.get("nit", ""),
        "city":                features.get("city", ""),
        "cuisine_type":        features.get("cuisine_type", ""),
        "features":            safe_features,
        "payment_methods":     features.get("payment_methods", []),
        "payment_instructions":features.get("payment_instructions", {}),
        "google_maps_url":     features.get("google_maps_url", ""),
        "bot_active":          features.get("bot_active", True),
        "upsell_active":       features.get("upsell_active", True),
        "domicilio_active":    features.get("domicilio_active", True),
        "recoger_active":      features.get("recoger_active", True),
        "delivery_fee":        features.get("delivery_fee", 0),
        "min_order":           features.get("min_order", 0),
        "delivery_radius_km":  features.get("delivery_radius_km", 5),
        "timezone":            features.get("timezone", "America/Bogota"),
        "currency":            features.get("currency", "COP"),
        "locale":              features.get("locale", "es-CO"),
        "latitude":            restaurant.get("latitude"),
        "longitude":           restaurant.get("longitude"),
        # Catálogo visual v2 — Fase 1
        "bot_visual_menu":     features.get("bot_visual_menu", False),
        "catalog_v2_enabled":  features.get("catalog_v2_enabled", True),
        # Voice notes transcription — opt-in, default OFF
        "bot_voice_notes":     features.get("bot_voice_notes", False),
        # DIAN electronic invoicing — opt-in, default OFF (requires folio purchase ~$400K COP)
        "dian_enabled":        features.get("dian_enabled", False),
        # Wompi config (sensitive: secret never returned plaintext)
        "wompi":               safe_features.get("wompi", {}),
    }


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

    features = _parse_features(restaurant)
    return _build_settings_response(restaurant, features)


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
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    body = await request.json()

    # ── Validation ────────────────────────────────────────────────────
    new_name = body.get("name")
    if new_name is not None and not str(new_name).strip():
        raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")

    current_features = _parse_features(restaurant)

    # ── Features-level fields (stored in organizations.features JSONB) ─
    _features_updatable = [
        "payment_methods", "payment_instructions", "google_maps_url", "bot_active",
        "upsell_active", "domicilio_active", "recoger_active",
        "delivery_fee", "min_order", "delivery_radius_km", "delivery_message",
        "pickup_message", "welcome_message",
        "timezone", "currency", "locale",
        # Catálogo visual v2 — Fase 1
        "bot_visual_menu", "catalog_v2_enabled",
        # Voice notes transcription — opt-in, default OFF
        "bot_voice_notes",
        # DIAN electronic invoicing — opt-in, default OFF
        "dian_enabled",
        # Extended info — no dedicated DB column; live in features JSONB
        "nit", "city", "cuisine_type", "notifications",
        # Wompi per-restaurant credentials (handled with secret preservation below)
        "wompi",
    ]
    _STR_FIELD_MAX = {
        "welcome_message": 500,
        "delivery_message": 500,
        "pickup_message": 500,
        "payment_instructions": 2000,
    }
    features_patch: dict = {}
    for key in _features_updatable:
        if key in body:
            val = body[key]
            if isinstance(val, str) and key in _STR_FIELD_MAX and len(val) > _STR_FIELD_MAX[key]:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key}: máximo {_STR_FIELD_MAX[key]} caracteres",
                )
            features_patch[key] = val

    # ── Wompi: preserve existing integrity_secret when client sends an empty
    # or masked value (form was loaded with masked placeholder and admin only
    # touched the public_key). Drops the wompi key entirely if the inbound
    # payload is malformed.
    if "wompi" in features_patch:
        inbound = features_patch.get("wompi") or {}
        if not isinstance(inbound, dict):
            features_patch.pop("wompi", None)
        else:
            existing = current_features.get("wompi") or {}
            if not isinstance(existing, dict):
                existing = {}
            new_pk = (inbound.get("public_key") or "").strip()
            new_secret_raw = inbound.get("integrity_secret")
            new_secret = (new_secret_raw or "").strip() if isinstance(new_secret_raw, str) else ""
            # If client sent no secret OR a masked placeholder, keep existing
            if not new_secret or new_secret.startswith(("xxxx", "****", "•")):
                preserved_secret = (existing.get("integrity_secret") or "").strip()
            else:
                preserved_secret = new_secret
            features_patch["wompi"] = {
                "public_key":       new_pk,
                "integrity_secret": preserved_secret,
            }

    lat = float(body["latitude"]) if "latitude" in body and body["latitude"] not in [None, ""] else None
    lon = float(body["longitude"]) if "longitude" in body and body["longitude"] not in [None, ""] else None

    # ── Persist to DB ─────────────────────────────────────────────────
    with tenant_scope(restaurant["id"]):
        # 1. Merge features patch (preserves existing keys not in patch)
        if features_patch:
            current_features = await restaurant_repo.db_merge_restaurant_features(
                restaurant["id"], features_patch
            )

        # 2. Location-level fields: name, address, opening_hours, lat/lon
        _location_updates: dict = {}
        if new_name is not None:
            _location_updates["name"] = str(new_name).strip()
        if "address" in body and body["address"] is not None:
            _location_updates["address"] = body["address"]
        if "opening_hours" in body and isinstance(body["opening_hours"], dict):
            _location_updates["opening_hours"] = body["opening_hours"]
        if lat is not None:
            _location_updates["latitude"] = lat
        if lon is not None:
            _location_updates["longitude"] = lon

        if _location_updates:
            await restaurant_repo.db_update_location(restaurant["id"], **_location_updates)

    # Re-fetch to return authoritative state
    updated = await db.db_get_restaurant_by_id(restaurant["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    final_features = _parse_features(updated)
    return _build_settings_response(updated, final_features)


# ── PAUSE / RESUME ───────────────────────────────────────────────────


class _PauseBody(BaseModel):
    paused: bool


@router.post("/api/settings/pause")
async def pause_restaurant(body: _PauseBody, request: Request):
    """Pause or resume the restaurant's WhatsApp bot and operations.

    Sets features.bot_active = false + paused_at / paused_by when paused=true.
    Clears those keys when paused=false.

    Auth: owner or admin only (gerente is excluded — restaurant-level config).
    """
    user = await get_current_user(request)
    role = (user.get("role") or "").lower()
    allowed_roles = {"owner", "admin"}
    # role field may be a comma-separated list when staff has multiple roles
    user_roles = {r.strip() for r in role.split(",") if r.strip()}
    if not (user_roles & allowed_roles):
        raise HTTPException(
            status_code=403,
            detail="Solo owner o admin pueden pausar/reanudar el restaurante",
        )

    branch_id = user.get("branch_id")
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit() and role in ("owner", "admin"):
        branch_id = int(branch_header)

    restaurant = await db.db_get_restaurant_by_id(branch_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    restaurant_id = restaurant["id"]

    if body.paused:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {
            "bot_active": False,
            "paused_at": now_iso,
            "paused_by": user.get("username") or "",
        }
    else:
        # Unpause: re-activate and clear pause metadata
        patch = {
            "bot_active": True,
            "paused_at": None,
            "paused_by": None,
        }

    with tenant_scope(restaurant_id):
        await restaurant_repo.db_merge_restaurant_features(restaurant_id, patch)

    log.info(
        "settings.pause" if body.paused else "settings.unpause",
        restaurant_id=restaurant_id,
        paused_by=user.get("username"),
    )

    # Re-fetch to return authoritative state
    updated = await db.db_get_restaurant_by_id(restaurant_id)
    features = _parse_features(updated or {})

    return {
        "paused": not features.get("bot_active", True),
        "paused_at": features.get("paused_at"),
        "paused_by": features.get("paused_by"),
    }


# ── PHONE BLOCKLIST (admin manual) ────────────────────────────────────


def _normalize_phone(phone: str) -> str:
    """Strip whitespace, '+' and dashes — keep only digits."""
    return (phone or "").strip().replace(" ", "").replace("+", "").replace("-", "")


def _require_admin_role(user: dict, action: str) -> None:
    """Raise 403 unless user.role contains owner or admin."""
    role = (user.get("role") or "").lower()
    user_roles = {r.strip() for r in role.split(",") if r.strip()}
    if not (user_roles & {"owner", "admin"}):
        raise HTTPException(
            status_code=403,
            detail=f"Solo owner o admin pueden {action}",
        )


def _resolve_org_id(restaurant: dict) -> int:
    """Return the org_id (canonical tenant key) from a restaurant dict."""
    org_id = restaurant.get("org_id") or restaurant.get("id")
    if org_id is None:
        raise HTTPException(status_code=500, detail="Restaurante sin org_id")
    return int(org_id)


class _BlockPhoneBody(BaseModel):
    phone: str
    reason: str = ""
    hours: int = 24


@router.get("/api/admin/blocklist")
async def list_blocklist(restaurant: dict = Depends(get_current_restaurant)):
    """List currently-active phone blocks for this org. Admin-facing.

    Returns up to 200 active blocks (blocked_until > NOW()), ordered by
    expiry descending. The phone field is included as-is; phone_obf is
    a UI-friendly masked version (e.g. ***1234).
    """
    from app.repositories.phone_blocklist_repo import list_active_blocks_for_org

    org_id = _resolve_org_id(restaurant)
    with tenant_scope(org_id):
        items = await list_active_blocks_for_org(org_id, limit=200)
    return {"items": items, "total": len(items)}


@router.post("/api/admin/blocklist")
async def block_phone(body: _BlockPhoneBody, request: Request):
    """Block a phone manually for N hours. Owner/admin only.

    Validation:
      - phone: 7-15 digits (international format, no '+').
      - hours: 1-720 (max 30 days).
      - reason: free text, capped at 200 chars.
    """
    user = await get_current_user(request)
    _require_admin_role(user, "bloquear teléfonos")

    phone = _normalize_phone(body.phone)
    if not phone.isdigit() or len(phone) < 7 or len(phone) > 15:
        raise HTTPException(status_code=400, detail="Phone debe tener 7-15 dígitos")
    if not isinstance(body.hours, int) or body.hours < 1 or body.hours > 720:
        raise HTTPException(status_code=400, detail="Hours debe estar entre 1 y 720")

    restaurant = await get_current_restaurant(request)
    org_id = _resolve_org_id(restaurant)
    blocker = user.get("username") or "admin"
    reason = (body.reason or "manual_admin")[:200]

    from app.repositories.phone_blocklist_repo import add_to_blocklist
    with tenant_scope(org_id):
        await add_to_blocklist(
            phone=phone,
            org_id=org_id,
            reason=reason,
            blocked_by=blocker,
            hours=body.hours,
        )

    log.info(
        "admin.phone_blocked",
        phone_obf="***" + phone[-4:],
        by=blocker,
        org_id=org_id,
        hours=body.hours,
    )
    return {"success": True, "phone": phone, "hours": body.hours}


@router.delete("/api/admin/blocklist/{phone}")
async def unblock_phone(phone: str, request: Request):
    """Remove a phone from the blocklist. Owner/admin only.

    Returns 404 if the phone was not currently blocked for this org.
    """
    user = await get_current_user(request)
    _require_admin_role(user, "desbloquear teléfonos")

    phone_clean = _normalize_phone(phone)
    if not phone_clean.isdigit() or len(phone_clean) < 7 or len(phone_clean) > 15:
        raise HTTPException(status_code=400, detail="Phone inválido")

    restaurant = await get_current_restaurant(request)
    org_id = _resolve_org_id(restaurant)

    from app.repositories.phone_blocklist_repo import remove_from_blocklist
    with tenant_scope(org_id):
        removed = await remove_from_blocklist(phone_clean, org_id)

    if not removed:
        raise HTTPException(status_code=404, detail="Phone no estaba bloqueado")

    log.info(
        "admin.phone_unblocked",
        phone_obf="***" + phone_clean[-4:],
        by=user.get("username") or "admin",
        org_id=org_id,
    )
    return {"success": True, "phone": phone_clean}


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

    with tenant_scope(restaurant_id):
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
        "owner_phone": features.get("owner_phone"),
        "timezone": features.get("timezone") or "America/Bogota",
    }

    return {"reports": reports, "settings": settings}


@router.patch("/api/weekly-reports/settings")
async def patch_weekly_report_settings(
    body: WeeklyReportSettings,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Update weekly report settings (enabled flag, owner_phone, timezone)."""
    restaurant_id = restaurant["id"]

    with tenant_scope(restaurant_id):
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
        with bypass_tenant_scope("onboarding_has_staff"):
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

    role = user.get("role", "")
    branch_header = request.headers.get("X-Branch-ID")

    # For owner/admin: respect X-Branch-ID. If they didn't explicitly pick a
    # sede (no header, 'matriz', 'all'), show all sedes of the org. The old
    # fallback to user.branch_id assumed branch_id == location_id, which broke
    # post-Wave-2 (user.branch_id is the org_id, not a location_id, so
    # filtering downstream by location_id matched nothing).
    if role in ("owner", "admin"):
        if branch_header == "all":
            branch_id = "all"
        elif branch_header and branch_header.isdigit():
            branch_id = int(branch_header)
        else:
            # Default for owners/admins: cross-sede view ('all'). bot_number
            # filter (resolved below) provides tenant scoping.
            branch_id = "all"
    else:
        # gerente / staff: use "all" so that bot_number does the tenant scoping.
        # user["branch_id"] stores org_id (not location_id) for staff users —
        # passing it as location_id filter would return 0 rows post-Wave-2.
        branch_id = "all"

    bot_number = None
    if branch_id and branch_id != "all":
        r = await db.db_get_restaurant_by_id(branch_id)
        if r:
            bot_number = r.get("whatsapp_number")
    elif branch_id == "all":
        r = await db.db_get_restaurant_by_id(user.get("branch_id"))
        if r:
            bot_number = r.get("whatsapp_number")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
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
                log.warning("dashboard.table_order_items_parse_failed", base_id=base_id)

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
    user = await get_current_user(request)
    body = await request.json()
    new_status = body.get("status", "")
    if not new_status:
        raise HTTPException(status_code=400, detail="status requerido")

    # Pre-resolve the order's tenant before entering scope for the update.
    # Wave-2: restaurant_id column dropped in 0038; use org_id instead.
    with bypass_tenant_scope("settings_update_order_status: pre-resolve order tenant"):
        order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    order_org_id = order.get("org_id")
    user_location_id = user.get("branch_id") or user.get("restaurant_id")
    if user_location_id:
        user_rest = await db.db_get_restaurant_by_id(user_location_id)
        user_org_id = (user_rest or {}).get("org_id") or user_location_id
    else:
        user_org_id = None

    if order_org_id and user_org_id and int(order_org_id) != int(user_org_id):
        raise HTTPException(status_code=403, detail="La orden no pertenece a tu sucursal")

    scope_rid = int(order_org_id) if order_org_id else int(user_org_id) if user_org_id else None
    if not scope_rid:
        raise HTTPException(status_code=500, detail="No se pudo resolver tenant de la orden")

    with tenant_scope(scope_rid):
        await db.db_update_order_status(order_id, new_status)
    return {"success": True}


@router.get("/api/table-sessions/closed")
async def get_closed_sessions(request: Request, hours: int = 24):
    hours = max(1, min(hours, 720))  # clamp: 1h – 30 days
    branch_id, bot_number, _, _ = await get_dashboard_filters(request, "today")

    try:
        # db_get_closed_sessions uses tenant_connection() — must be wrapped in tenant_scope.
        # branch_id may be an int, "all", or None; fall back to bypass when cross-tenant.
        # Wave-2: branch_id from get_dashboard_filters is a LOCATION_ID (from
        # user.branch_id or X-Branch-ID header). tenant_scope() requires an
        # org_id; we resolve it via db_get_restaurant_by_id which normalizes
        # `id` to org_id regardless of which key was passed in.
        if isinstance(branch_id, int):
            branch_rest = await db.db_get_restaurant_by_id(branch_id)
            org_id_for_scope = branch_rest["id"] if branch_rest else branch_id
            with tenant_scope(org_id_for_scope):
                rows = await tr.db_get_closed_sessions(hours, bot_number)
        else:
            from app.services.tenant_context import bypass_tenant_scope as _bypass
            with _bypass("get_closed_sessions: cross-tenant dashboard view"):
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
        log.exception("dashboard.reservations_load_failed", bot_number=bot_number)

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


@router.get("/api/dashboard/pedidos-rescatados")
async def get_pedidos_rescatados(
    request: Request,
    period: str = "mtd",
):
    """
    North-star metric: count of bot-originated orders for the current tenant.

    period values:
      mtd  — month-to-date (1st of current month → today)
      7d   — last 7 days
      30d  — last 30 days

    Returns:
      {
        "period": "mtd",
        "count": 125,           # total (delivery + table)
        "delivery": 80,
        "table": 45,
        "prev_count": 112,      # same window shifted back one period (for delta)
        "delta_pct": 11.6       # positive = growth
      }

    Requires: active restaurant Bearer token.
    """
    from datetime import date, timedelta
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    restaurant = await get_current_restaurant(request)
    today = date.today()
    if period == "mtd":
        period_start = today.replace(day=1)
        period_end   = today
        prev_end   = period_start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
    elif period == "7d":
        period_start = today - timedelta(days=6)
        period_end   = today
        prev_end   = period_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=6)
    else:  # 30d default
        period_start = today - timedelta(days=29)
        period_end   = today
        prev_end   = period_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=29)

    org_id = restaurant["id"]
    try:
        with tenant_scope(org_id):
            current = await db_count_pedidos_rescatados(period_start, period_end)
            prev    = await db_count_pedidos_rescatados(prev_start, prev_end)
    except Exception as exc:
        log.exception("dashboard.pedidos_rescatados_failed", org_id=org_id)
        raise HTTPException(status_code=500, detail="Error al calcular pedidos rescatados")

    delta_pct = None
    if prev["count"] and prev["count"] > 0:
        delta_pct = round((current["count"] - prev["count"]) / prev["count"] * 100, 1)  # JSON boundary

    return {
        "period": period,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "count": current["count"],
        "delivery": current["delivery"],
        "table": current["table"],
        "prev_count": prev["count"],
        "delta_pct": delta_pct,
    }


async def _verify_session_ownership_and_scope(session_id: int, restaurant: dict) -> dict:
    """Load a table_session, verify it belongs to the caller's org, return the row.

    Wave-2: loads the session inside bypass_tenant_scope (ID-based lookup with
    unknown org), then verifies org_id ownership before doing any mutation.
    Returns the session dict on success; raises 404 if not found or cross-org.
    """
    with bypass_tenant_scope("session_ownership_check: load session by ID for ownership verification"):
        session_row, _history = await tr.db_get_session_with_history(session_id)
    if not session_row:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    caller_org_id = restaurant.get("org_id") or restaurant.get("id")
    session_org_id = session_row.get("org_id")

    if session_org_id is None or caller_org_id is None:
        log.warning(
            "settings.session_missing_org_id",
            session_id=session_id,
            session_org_id=session_org_id,
            caller_org_id=caller_org_id,
        )
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if int(session_org_id) != int(caller_org_id):
        log.warning(
            "settings.session_idor_attempt",
            session_id=session_id,
            session_org_id=session_org_id,
            caller_org_id=caller_org_id,
        )
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    return session_row


@router.get("/api/table-sessions/{session_id}/history")
async def get_session_history(
    request: Request,
    session_id: int,
    restaurant: dict = Depends(get_current_restaurant),
):
    with bypass_tenant_scope("get_session_history: session lookup by ID, tenant unknown"):
        session, history = await tr.db_get_session_with_history(session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")

    caller_org_id = restaurant.get("org_id") or restaurant.get("id")
    session_org_id = session.get("org_id")
    if session_org_id is None or caller_org_id is None:
        log.warning(
            "settings.session_history_missing_org_id",
            session_id=session_id,
            session_org_id=session_org_id,
            caller_org_id=caller_org_id,
        )
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if int(session_org_id) != int(caller_org_id):
        log.warning(
            "settings.session_history_idor",
            session_id=session_id,
            session_org_id=session_org_id,
            caller_org_id=caller_org_id,
        )
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if session.get("started_at"): session["started_at"] = session["started_at"].isoformat()
    if session.get("closed_at"): session["closed_at"] = session["closed_at"].isoformat()
    return {"session": session, "history": history}


@router.post("/api/table-sessions/{session_id}/reopen")
async def reopen_session(
    request: Request,
    session_id: int,
    restaurant: dict = Depends(get_current_restaurant),
):
    await _verify_session_ownership_and_scope(session_id, restaurant)
    with bypass_tenant_scope("reopen_session: session update by ID after ownership verified"):
        await tr.db_reopen_session(session_id)
    return {"success": True}


@router.post("/api/table-sessions/{session_id}/alert-waiter")
async def session_alert_waiter(
    request: Request,
    session_id: int,
    restaurant: dict = Depends(get_current_restaurant),
):
    body = await request.json()
    await _verify_session_ownership_and_scope(session_id, restaurant)
    with bypass_tenant_scope("session_alert_waiter: alert insert after ownership verified"):
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
async def ai_proxy(payload: _AIProxyRequest, request: Request, _user: str = Depends(require_auth)):
    """
    Proxy autenticado para llamadas al modelo de IA desde el dashboard.
    El ANTHROPIC_API_KEY vive solo en el servidor — nunca se expone al cliente.
    Requiere Bearer token de admin válido.
    Rate limit: 20 req/min por usuario autenticado.
    """
    # ── Rate limit: 20 req/min por usuario ───────────────────────────────────────
    rl_key = f"ai_proxy:{_user}"
    allowed = await state_store.rate_limit_check(rl_key, max_requests=20, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Demasiadas solicitudes al proxy de IA. Espera un momento e intenta de nuevo.",
        )

    max_tok = min(max(1, payload.max_tokens), 1000)  # clamp 1–1000 (conservador)
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
    _FOLDER_SUFFIX_ALLOWLIST = {"menu", "gallery", "logo"}
    _raw_suffix = (body.folder_suffix or "menu").strip() or "menu"
    folder_suffix = _raw_suffix if _raw_suffix in _FOLDER_SUFFIX_ALLOWLIST else "menu"

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


# ── Catálogo Visual v2 — Visual onboarding gap report ────────────────────────

@router.get("/api/menu/missing-photos")
async def get_dishes_missing_photos(
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    List dishes in this restaurant's menu that lack image_url.

    Used by the admin menu editor to surface a visual-onboarding gap:
    "X de Y platos tienen foto. Faltan: [list]". Without this, an admin
    can believe they have a "menú visual" while only a few dishes have
    images uploaded.

    Returns:
        {
            "total_dishes":  int — total dishes across all categories,
            "with_photo":    int — dishes that have a non-empty image_url,
            "missing_count": int — dishes without image_url,
            "missing":       [{category, name, price}, ...],
        }

    Auth: admin/owner Bearer token.
    """
    raw_menu = restaurant.get("menu") or {}

    # Tolerate string-encoded JSON (some DB code paths return raw str).
    if isinstance(raw_menu, str):
        try:
            raw_menu = json.loads(raw_menu)
        except Exception:
            raw_menu = {}

    # Fallback: if the dict for some reason doesn't carry the menu, look it
    # up by whatsapp_number. Mirrors the pattern in dashboard_data().
    if not raw_menu and restaurant.get("whatsapp_number"):
        try:
            raw_menu = await db.db_get_menu(restaurant["whatsapp_number"]) or {}
        except Exception as exc:
            log.warning(
                "menu.missing_photos.fallback_lookup_failed",
                restaurant_id=restaurant.get("id"),
                error=str(exc),
            )
            raw_menu = {}

    if not isinstance(raw_menu, dict):
        raw_menu = {}

    total_dishes = 0
    missing: list[dict] = []
    for category, dishes in raw_menu.items():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if not isinstance(dish, dict):
                continue
            total_dishes += 1
            image_url = dish.get("image_url")
            if not image_url or not str(image_url).strip():
                missing.append({
                    "category": category,
                    "name":     dish.get("name", ""),
                    "price":    dish.get("price", 0),
                })

    return {
        "total_dishes":  total_dishes,
        "with_photo":    total_dishes - len(missing),
        "missing_count": len(missing),
        "missing":       missing,
    }


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

    with tenant_scope(restaurant_id):
        per_dish = await menu_analytics_repo.get_dish_analytics(restaurant_id, days)
        matrix   = await menu_analytics_repo.get_menu_engineering_matrix(restaurant_id, days)

    return {"per_dish": per_dish, "matrix": matrix}


@router.get("/api/menu/analytics/export")
async def menu_analytics_export(
    days: int = 30,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Export menu engineering data as CSV download.
    Aggregates per_dish stats + quadrant classification from get_menu_engineering_matrix.
    Auth: admin/owner Bearer token.
    """
    import io
    import csv
    from fastapi.responses import StreamingResponse
    from app.repositories import menu_analytics_repo

    days = max(1, min(days, 365))
    restaurant_id = restaurant["id"]

    with tenant_scope(restaurant_id):
        per_dish = await menu_analytics_repo.get_dish_analytics(restaurant_id, days)
        matrix   = await menu_analytics_repo.get_menu_engineering_matrix(restaurant_id, days)

    # Build quadrant lookup: dish_name -> quadrant label
    quad_lookup: dict[str, str] = {}
    for q_name, label in (
        ("stars", "Star"),
        ("puzzles", "Puzzle"),
        ("plowhorses", "Plowhorse"),
        ("dogs", "Dog"),
    ):
        for entry in matrix.get(q_name, []) or []:
            quad_lookup[entry["dish_name"]] = label

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Plato",
        "Vistas",
        "Apertura modal",
        "Agregados al carrito",
        "Pedidos",
        "Conversión vista→carrito (%)",
        "Conversión carrito→pedido (%)",
        "Cuadrante",
    ])
    for row in per_dish:
        writer.writerow([
            row.get("dish_name", ""),
            row.get("views", 0),
            row.get("modal_opens", 0),
            row.get("add_to_carts", 0),
            row.get("orders", 0),
            round((row.get("cart_conversion_rate", 0) or 0) * 100, 2),
            round((row.get("order_conversion_rate", 0) or 0) * 100, 2),
            quad_lookup.get(row.get("dish_name", ""), ""),
        ])

    buf.seek(0)
    filename = f"menu-engineering-{restaurant_id}-{days}d.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
