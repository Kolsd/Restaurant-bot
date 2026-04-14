"""
Dashboard page router: serves HTML pages, the public restaurant/menu APIs,
the geocode helper, and the service worker.

Business logic is split into:
  - app.routes.auth_routes   → /api/auth/*, /api/admin/*
  - app.routes.settings_routes → /api/settings, /api/dashboard/*, /api/ai/proxy,
                                  /api/orders/{id}/status, /api/table-sessions/*
  - app.routes.team_routes   → /api/team/*
"""
import json
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pathlib import Path
from pydantic import BaseModel, field_validator

from app.services import database as db
from app.repositories import restaurant_repo
from app.services import state_store
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"


# ── GEOCODE HELPER (shared — imported by auth_routes and team_routes) ─

async def geocode_address(address: str) -> tuple:
    """
    Geocodifica una dirección. Usa Nominatim (OpenStreetMap) como primario,
    con sesgo a Colombia, y sin API key requerida.
    Retorna (lat, lon, display_name) o (None, None, None).
    """
    headers = {"User-Agent": "Mesio-Bot/1.0 (contacto@mesioai.com)"}
    query = address if any(c in address.lower() for c in ("colombia", "bogotá", "medellin", "cali")) else f"{address}, Colombia"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "co"},
                headers=headers,
            )
            if r.status_code == 200:
                results = r.json()
                if results:
                    return float(results[0]["lat"]), float(results[0]["lon"]), results[0].get("display_name", "")
    except Exception:
        pass
    return None, None, None


# ── SERVICE WORKER (must be served at root scope, not /static/) ───────

@router.get("/sw.js")
async def service_worker():
    content = (STATIC / "js" / "sw.js").read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# ── HTML PAGES ────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return (STATIC / "html" / "login.html").read_text(encoding="utf-8")

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return (STATIC / "html" / "dashboard.html").read_text(encoding="utf-8")

@router.get("/demo", response_class=HTMLResponse)
@router.get("/dashboard-demo", response_class=HTMLResponse)
async def demo_page():
    return (STATIC / "html" / "dashboard-demo.html").read_text(encoding="utf-8")

@router.get("/landing", response_class=HTMLResponse)
async def landing_page():
    return (STATIC / "html" / "landing.html").read_text(encoding="utf-8")

@router.get("/", response_class=HTMLResponse)
async def root_redirect():
    return (STATIC / "html" / "landing.html").read_text(encoding="utf-8")

@router.get("/superadmin", response_class=HTMLResponse)
async def superadmin_page():
    p = STATIC / "html" / "superadmin.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>")

@router.get("/staff")
async def staff_portal_redirect(request: Request):
    r = request.query_params.get("r", "")
    target = f"/login?r={r}" if r else "/login"
    return RedirectResponse(url=target, status_code=302)

@router.get("/mesero", response_class=HTMLResponse)
async def mesero_page():
    return (STATIC / "html" / "mesero.html").read_text(encoding="utf-8")

@router.get("/caja", response_class=HTMLResponse)
async def caja_page():
    p = STATIC / "html" / "caja.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Caja no disponible</h1>")

@router.get("/crm", response_class=HTMLResponse)
async def crm_page():
    return (STATIC / "html" / "crm.html").read_text(encoding="utf-8")

@router.get("/demo-chat", response_class=HTMLResponse)
async def demo_chat_bot_page():
    p = STATIC / "html" / "demo-chat.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Falta el archivo demo-chat.html en la carpeta static</h1>", status_code=404)

@router.get("/catalog", response_class=HTMLResponse)
async def catalog_page():
    p = STATIC / "html" / "catalog.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Catálogo no disponible</h1>")

@router.get("/privacidad", response_class=HTMLResponse)
async def privacidad_page():
    return (STATIC / "html" / "privacidad.html").read_text(encoding="utf-8")

@router.get("/terminos", response_class=HTMLResponse)
async def terminos_page():
    return (STATIC / "html" / "terminos.html").read_text(encoding="utf-8")

@router.get("/billing", response_class=HTMLResponse)
async def billing_page():
    p = STATIC / "html" / "billing.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Billing no disponible</h1>")

@router.get("/domiciliario", response_class=HTMLResponse)
async def domiciliario_page():
    p = STATIC / "html" / "domiciliario.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Página no encontrada</h1>", status_code=404)

@router.get("/staff-hq", response_class=HTMLResponse)
async def staff_hq_page():
    p = STATIC / "html" / "staff-hq.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>", status_code=404)

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    p = STATIC / "html" / "settings.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Settings no disponible</h1>")


# ── PUBLIC APIs ───────────────────────────────────────────────────────

@router.get("/api/public/restaurant-info")
async def public_restaurant_info(id: int):
    """Return the restaurant name for a given restaurant ID (public, read-only)."""
    restaurant = await db.db_get_restaurant_by_id(id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    return {"name": restaurant.get("name", "")}


@router.get("/api/public/menu/{bot_number}")
async def get_public_menu(bot_number: str):
    normalized = bot_number.replace("+", "").replace(" ", "").strip()
    data = await restaurant_repo.db_get_public_menu_data(normalized)
    if not data:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    menu_data = data["menu"]
    if (not menu_data or menu_data == '{}' or menu_data == "{}") and data["parent_menu"]:
        menu_data = data["parent_menu"]
    if isinstance(menu_data, str):
        try:
            menu_data = json.loads(menu_data)
            if isinstance(menu_data, str):
                menu_data = json.loads(menu_data)
        except Exception:
            menu_data = {}
    elif not menu_data:
        menu_data = {}

    features = data["features"]
    if (not features or features == '{}' or features == "{}") and data["parent_features"]:
        features = data["parent_features"]
    if isinstance(features, str):
        try:
            features = json.loads(features)
            if isinstance(features, str):
                features = json.loads(features)
        except Exception:
            features = {}
    elif not features:
        features = {}

    return {
        "restaurant_name": data["name"],
        "menu": menu_data,
        "availability": data["availability"],
        "bot_number": bot_number,
        "locale": features.get("locale", "es-CO"),
        "currency": features.get("currency", "COP"),
        "catalog_v2_enabled": bool(features.get("catalog_v2_enabled", True)),
        "bot_visual_menu": bool(features.get("bot_visual_menu", False)),
    }


@router.get("/api/geocode")
async def geocode_endpoint(address: str):
    lat, lon, display = await geocode_address(address)
    if lat is None:
        raise HTTPException(status_code=404, detail="No se encontró la dirección.")
    return {
        "latitude": lat,
        "longitude": lon,
        "display_name": display,
        "maps_url": f"https://www.google.com/maps?q={lat},{lon}"
    }


# ── Catalog v2: analytics tracking (fire-and-forget, real DB insert) ──────────

_VALID_TRACK_EVENTS = frozenset({"view", "modal_open", "add_to_cart", "ordered"})


class MenuTrackBody(BaseModel):
    dish_name:  str
    event_type: str
    bot_number: str
    phone:      str | None = None

    @field_validator("dish_name")
    @classmethod
    def _dish_name_len(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("dish_name required")
        return v.strip()[:255]

    @field_validator("event_type")
    @classmethod
    def _valid_event(cls, v: str) -> str:
        if v not in _VALID_TRACK_EVENTS:
            raise ValueError(f"event_type must be one of {sorted(_VALID_TRACK_EVENTS)}")
        return v

    @field_validator("bot_number")
    @classmethod
    def _bot_number_len(cls, v: str) -> str:
        if len(v) > 20:
            raise ValueError("bot_number max 20 chars")
        return v


@router.post("/api/public/menu/track")
async def menu_track(body: MenuTrackBody):
    """
    Fire-and-forget analytics tracking for catalog v2 events.
    Rate-limited to 120 req/min per bot_number.
    Always returns 200 (sendBeacon callers cannot handle 4xx/5xx).
    """
    from app.repositories import menu_analytics_repo

    rate_key = f"catalog_track:{body.bot_number}"
    allowed = await state_store.rate_limit_check(rate_key, max_requests=120, window_seconds=60)
    if not allowed:
        log.warning("catalog.track.rate_limited", bot_number=body.bot_number)
        return {"ok": True}

    # Resolve restaurant_id from bot_number — fire-and-forget on miss
    restaurant = await restaurant_repo.db_get_restaurant_by_bot_number(body.bot_number)
    if not restaurant:
        log.info(
            "catalog.track.unknown_bot",
            bot_number=body.bot_number,
            dish_name=body.dish_name,
            event_type=body.event_type,
        )
        return {"ok": True}

    await menu_analytics_repo.record_event(
        restaurant_id=restaurant["id"],
        dish_name=body.dish_name,
        event_type=body.event_type,
        phone=body.phone,
        bot_number=body.bot_number,
    )
    return {"ok": True}
