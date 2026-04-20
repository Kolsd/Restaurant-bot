"""
Dashboard page router: serves HTML pages, the public restaurant/menu APIs,
the geocode helper, and the service worker.

Business logic is split into:
  - app.routes.auth_routes   → /api/auth/*, /api/admin/*
  - app.routes.settings_routes → /api/settings, /api/dashboard/*, /api/ai/proxy,
                                  /api/orders/{id}/status, /api/table-sessions/*
  - app.routes.team_routes   → /api/team/*
"""
import html as _html
import json
import os
import re as _re
import urllib.parse
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pathlib import Path
from pydantic import BaseModel, field_validator

from app.services import database as db
from app.repositories import restaurant_repo
from app.services import state_store
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

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

@router.get("/staff-clock", response_class=HTMLResponse)
async def staff_clock_page():
    """Alias of /staff-hq for the new design naming convention."""
    p = STATIC / "html" / "staff-hq.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Staff Clock no disponible</h1>", status_code=404)

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    p = STATIC / "html" / "settings.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Settings no disponible</h1>")

@router.get("/floorplan", response_class=HTMLResponse)
async def floorplan_page():
    p = STATIC / "html" / "floorplan.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Floorplan no disponible</h1>", status_code=404)

@router.get("/equipo", response_class=HTMLResponse)
async def equipo_page():
    p = STATIC / "html" / "equipo.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Equipo no disponible</h1>", status_code=404)

@router.get("/pedidos", response_class=HTMLResponse)
async def pedidos_page():
    p = STATIC / "html" / "pedidos.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Pedidos no disponible</h1>", status_code=404)

@router.get("/reservaciones", response_class=HTMLResponse)
async def reservaciones_page():
    p = STATIC / "html" / "reservaciones.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Reservaciones no disponible</h1>", status_code=404)

@router.get("/menu-admin", response_class=HTMLResponse)
async def menu_admin_page():
    p = STATIC / "html" / "menu-admin.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Menu Admin no disponible</h1>", status_code=404)

@router.get("/menu-engineering", response_class=HTMLResponse)
async def menu_engineering_page():
    p = STATIC / "html" / "menu-engineering.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Menu Engineering no disponible</h1>", status_code=404)

@router.get("/nps", response_class=HTMLResponse)
async def nps_page():
    p = STATIC / "html" / "nps.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>NPS no disponible</h1>", status_code=404)

@router.get("/fidelizacion", response_class=HTMLResponse)
async def fidelizacion_page():
    p = STATIC / "html" / "fidelizacion.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Fidelización no disponible</h1>", status_code=404)

@router.get("/clientes-riesgo", response_class=HTMLResponse)
async def clientes_riesgo_page():
    p = STATIC / "html" / "clientes-riesgo.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Clientes en riesgo no disponible</h1>", status_code=404)

@router.get("/nomina", response_class=HTMLResponse)
async def nomina_page():
    p = STATIC / "html" / "nomina.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Nómina no disponible</h1>", status_code=404)

@router.get("/sucursales", response_class=HTMLResponse)
async def sucursales_page():
    p = STATIC / "html" / "sucursales.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Sucursales no disponible</h1>", status_code=404)


# ── PUBLIC APIs ───────────────────────────────────────────────────────

@router.get("/api/public/restaurant-info")
async def public_restaurant_info(id: int):
    """Return the restaurant name for a given restaurant ID (public, read-only)."""
    restaurant = await db.db_get_restaurant_by_id(id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    return {"name": restaurant.get("name", "")}


@router.get("/api/public/menu/{bot_number}")
async def get_public_menu(request: Request, bot_number: str):
    # ── Rate limit: 30 req/min por IP — previene harvesting masivo ─────────────
    client_ip = request.client.host if request.client else "unknown"
    allowed = await state_store.rate_limit_check(f"public_menu:{client_ip}", max_requests=30, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

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
async def geocode_endpoint(request: Request, address: str):
    """
    Proxy geocode a Nominatim. Requiere autenticación (Bearer token de admin/staff).
    Rate limit: 10 req/min por IP.
    """
    from app.routes.deps import require_auth
    await require_auth(request)

    # ── Rate limit: 10 req/min por IP ────────────────────────────────────────────
    client_ip = request.client.host if request.client else "unknown"
    allowed = await state_store.rate_limit_check(f"geocode:{client_ip}", max_requests=10, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes de geocodificación. Intenta más tarde.")

    lat, lon, display = await geocode_address(address)
    if lat is None:
        raise HTTPException(status_code=404, detail="No se encontró la dirección.")
    return {
        "latitude": lat,
        "longitude": lon,
        "display_name": display,
        "maps_url": f"https://www.google.com/maps?q={lat},{lon}"
    }


@router.get("/api/geocode/reverse")
async def geocode_reverse_endpoint(request: Request, lat: float, lon: float):
    """
    Proxy reverse geocode a Nominatim. Requiere autenticación (Bearer token de admin/staff).
    Rate limit: 10 req/min por IP (compartido con /api/geocode).
    El frontend debe usar este endpoint en lugar de llamar a Nominatim directamente.
    TODO (wave siguiente): migrar dashboard-features.js para usar este endpoint.
    """
    from app.routes.deps import require_auth
    await require_auth(request)

    client_ip = request.client.host if request.client else "unknown"
    allowed = await state_store.rate_limit_check(f"geocode:{client_ip}", max_requests=10, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes de geocodificación. Intenta más tarde.")

    headers = {"User-Agent": "Mesio-Bot/1.0 (contacto@mesioai.com)"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json"},
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "latitude": lat,
                    "longitude": lon,
                    "display_name": data.get("display_name", ""),
                }
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="No se encontró información para estas coordenadas.")


# ── Catalog v2: analytics tracking (fire-and-forget, real DB insert — Fase 5b) ─

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


# ── SEO / Growth routes (Catálogo v2 Fase 6) ─────────────────────────────────

_APP_DOMAIN = os.getenv("APP_DOMAIN", "mesioai.com")
_DISH_PAGE_TEMPLATE = (STATIC / "html" / "dish_page.html").read_text(encoding="utf-8")

# Fallback OG image (served from static or a CDN constant)
_OG_IMAGE_FALLBACK = f"https://{_APP_DOMAIN}/static/img/mesio-og-default.png"


def _slugify_dish(name: str) -> str:
    s = _re.sub(r'[^a-zA-Z0-9]+', '-', (name or '').lower()).strip('-')
    return s or 'plato'


def _find_dish_by_slug(menu: dict, dish_slug: str) -> dict | None:
    """Iterate all menu categories and return the first dish whose slug matches."""
    for dishes in menu.values():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if not isinstance(dish, dict):
                continue
            if _slugify_dish(dish.get("name", "")) == dish_slug:
                return dish
    return None


def _first_featured_image(menu: dict) -> str | None:
    """Return image_url of the first featured (or any) dish that has one."""
    # First pass: featured=True
    for dishes in menu.values():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if isinstance(dish, dict) and dish.get("featured") and dish.get("image_url"):
                return dish["image_url"]
    # Second pass: any dish with an image
    for dishes in menu.values():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if isinstance(dish, dict) and dish.get("image_url"):
                return dish["image_url"]
    return None


def _get_menu_from_data(data: dict) -> dict:
    """Resolve menu dict from restaurant data (handles inherited parent_menu)."""
    menu_data = data.get("menu") or data.get("parent_menu")
    if isinstance(menu_data, str):
        try:
            menu_data = json.loads(menu_data)
            if isinstance(menu_data, str):
                menu_data = json.loads(menu_data)
        except Exception:
            menu_data = {}
    return menu_data or {}


def _format_price(price, features: dict) -> str:
    currency = (features or {}).get("currency", "USD")
    locale   = (features or {}).get("locale", "en-US")
    try:
        p = int(price)
    except (TypeError, ValueError):
        return str(price or "")
    # Simple formatting: zero-decimal currencies
    zero_decimal = {"COP", "CLP", "JPY", "KRW", "VND", "PYG", "ISK"}
    if currency in zero_decimal:
        return f"${p:,}"
    return f"${p:,.2f}"


@router.get("/r/{slug}/menu", response_class=HTMLResponse)
async def seo_menu_page(slug: str):
    """
    Server-rendered menu page with Open Graph tags.
    OG crawlers (WhatsApp, Facebook) read the meta tags.
    Humans are redirected immediately to the JS catalog (/menu/{bot_number}).
    """
    data = await restaurant_repo.db_get_restaurant_by_slug(slug)
    if not data:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    name        = data.get("name") or "Restaurante"
    bot_number  = data.get("whatsapp_number") or ""
    features    = data.get("features") or {}
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except Exception:
            features = {}

    menu        = _get_menu_from_data(data)
    og_image    = _first_featured_image(menu)

    if og_image:
        try:
            from app.services.image_host import build_transform_url
            og_image = build_transform_url(og_image, "hero") or og_image
        except Exception:
            pass
    else:
        og_image = _OG_IMAGE_FALLBACK

    canonical   = f"https://{_APP_DOMAIN}/r/{_html.escape(slug)}/menu"
    redirect_to = f"/menu/{urllib.parse.quote(bot_number)}" if bot_number else f"/r/{slug}/menu"
    esc_name    = _html.escape(name)
    description = _html.escape(f"Menú de {name} — pide por WhatsApp")

    html_body = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Menú | {esc_name}</title>
  <meta property="og:type"        content="website">
  <meta property="og:title"       content="Menú | {esc_name}">
  <meta property="og:description" content="{description}">
  <meta property="og:image"       content="{_html.escape(og_image)}">
  <meta property="og:url"         content="{canonical}">
  <meta property="og:site_name"   content="{esc_name}">
  <meta name="twitter:card"       content="summary_large_image">
  <link rel="canonical"           href="{canonical}">
  <meta http-equiv="refresh"      content="0; url={_html.escape(redirect_to)}">
</head>
<body>
  <p>Redirigiendo al menú de <strong>{esc_name}</strong>…</p>
  <p><a href="{_html.escape(redirect_to)}">Haz clic aquí si no eres redirigido</a></p>
</body>
</html>"""
    return HTMLResponse(content=html_body, status_code=200)


@router.get("/r/{slug}/menu/{dish_slug}", response_class=HTMLResponse)
async def seo_dish_page(slug: str, dish_slug: str):
    """
    Server-rendered dish page with per-dish Open Graph tags and WhatsApp CTA.
    """
    data = await restaurant_repo.db_get_restaurant_by_slug(slug)
    if not data:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    name        = data.get("name") or "Restaurante"
    bot_number  = data.get("whatsapp_number") or ""
    features    = data.get("features") or {}
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except Exception:
            features = {}

    menu = _get_menu_from_data(data)
    dish = _find_dish_by_slug(menu, dish_slug)
    if not dish:
        raise HTTPException(status_code=404, detail="Plato no encontrado")

    dish_name   = dish.get("name", "Plato")
    description = dish.get("description", "")
    price       = dish.get("price", 0)
    image_url   = dish.get("image_url")
    currency    = (features or {}).get("currency", "USD")

    # OG image — use hero transform if Cloudinary
    og_image = image_url
    if og_image:
        try:
            from app.services.image_host import build_transform_url
            og_image = build_transform_url(og_image, "hero") or og_image
        except Exception:
            pass
    if not og_image:
        og_image = _OG_IMAGE_FALLBACK

    canonical    = f"https://{_APP_DOMAIN}/r/{_html.escape(slug)}/menu/{_html.escape(dish_slug)}"
    menu_url     = f"https://{_APP_DOMAIN}/r/{_html.escape(slug)}/menu"
    og_title     = f"{_html.escape(dish_name)} | {_html.escape(name)}"
    og_desc      = _html.escape((description or "")[:200])
    price_display = _format_price(price, features)

    # WhatsApp pre-filled link
    wa_text  = urllib.parse.quote(f"Hola, quiero pedir {dish_name}")
    wa_link  = f"https://wa.me/{urllib.parse.quote(bot_number.replace('+', ''))}?text={wa_text}" if bot_number else "#"

    # Dish image HTML — XSS-safe: all values escaped
    if image_url:
        dish_image_html = (
            f'<img class="dish-image" src="{_html.escape(image_url)}" '
            f'alt="{_html.escape(dish_name)}" loading="eager">'
        )
    else:
        dish_image_html = '<div class="placeholder-img">🍽️</div>'

    description_html = (
        f'<p class="description">{og_desc}</p>' if og_desc else ""
    )

    page = _DISH_PAGE_TEMPLATE.replace("{{DISH_NAME}}", _html.escape(dish_name))
    page = page.replace("{{RESTAURANT_NAME}}", _html.escape(name))
    page = page.replace("{{OG_TITLE}}", og_title)
    page = page.replace("{{OG_DESCRIPTION}}", og_desc)
    page = page.replace("{{OG_IMAGE}}", _html.escape(og_image))
    page = page.replace("{{OG_URL}}", canonical)
    page = page.replace("{{PRICE_AMOUNT}}", str(price))
    page = page.replace("{{PRICE_CURRENCY}}", _html.escape(currency))
    page = page.replace("{{DISH_IMAGE_HTML}}", dish_image_html)
    page = page.replace("{{PRICE_DISPLAY}}", _html.escape(price_display))
    page = page.replace("{{DESCRIPTION_HTML}}", description_html)
    page = page.replace("{{WA_LINK}}", _html.escape(wa_link))
    page = page.replace("{{MENU_URL}}", menu_url)

    return HTMLResponse(content=page, status_code=200)


@router.get("/sitemap-{restaurant_id}.xml")
async def restaurant_sitemap(restaurant_id: int):
    """
    Per-restaurant XML sitemap listing menu page + individual dish pages.
    Cached for 1 hour.
    """
    data = await db.db_get_restaurant_by_id(restaurant_id)
    if not data:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    slug     = data.get("slug") or f"r-{restaurant_id}"
    menu_raw = data.get("menu") or {}
    if isinstance(menu_raw, str):
        try:
            menu_raw = json.loads(menu_raw)
        except Exception:
            menu_raw = {}

    # Load availability to exclude out-of-stock dishes from the sitemap.
    # Public sitemap — cross-tenant by design (resolved from URL path param).
    availability: dict = {}
    try:
        with bypass_tenant_scope("restaurant_sitemap: public sitemap menu availability lookup"):
            availability = await db.db_get_menu_availability(restaurant_id) or {}
    except Exception:
        log.exception("sitemap.availability_load_failed", restaurant_id=restaurant_id)
        # availability is optional — fail open (sitemap still renders all dishes)

    base     = f"https://{_APP_DOMAIN}"
    menu_url = f"{base}/r/{slug}/menu"

    urls = [{"loc": menu_url, "changefreq": "weekly"}]
    for dishes in (menu_raw or {}).values():
        if not isinstance(dishes, list):
            continue
        for dish in dishes:
            if not isinstance(dish, dict) or not dish.get("name"):
                continue
            if dish.get("active") is False:
                continue
            # Exclude dishes that are explicitly marked unavailable in menu_availability
            dish_name = dish["name"]
            if availability.get(dish_name) is False:
                continue
            d_slug = _slugify_dish(dish_name)
            urls.append({
                "loc":        f"{base}/r/{slug}/menu/{d_slug}",
                "changefreq": "daily",
            })

    url_entries = "\n".join(
        f"  <url>\n"
        f"    <loc>{_html.escape(u['loc'])}</loc>\n"
        f"    <changefreq>{u['changefreq']}</changefreq>\n"
        f"  </url>"
        for u in urls
    )

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{url_entries}\n"
        "</urlset>"
    )

    return Response(
        content=xml_body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )
