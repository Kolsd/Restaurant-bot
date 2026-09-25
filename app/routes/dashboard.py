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
from fastapi.responses import HTMLResponse, Response
from pathlib import Path
from pydantic import BaseModel, field_validator

from app.services import database as db
from app.repositories import delivery_repo, restaurant_repo
from app.services import state_store
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"


# ── GEOCODE HELPER (shared — imported by auth_routes and team_routes) ─

async def geocode_address(address: str) -> tuple:
    """
    Geocodes an address. Uses Nominatim (OpenStreetMap) as the primary provider,
    biased toward Colombia, with no API key required.
    Returns (lat, lon, display_name) or (None, None, None).
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

@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page():
    return (STATIC / "html" / "reset-password.html").read_text(encoding="utf-8")

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return (STATIC / "html" / "dashboard.html").read_text(encoding="utf-8")

@router.get("/landing", response_class=HTMLResponse)
async def landing_page():
    return (STATIC / "html" / "landing.html").read_text(encoding="utf-8")

@router.get("/", response_class=HTMLResponse)
async def root_redirect():
    return (STATIC / "html" / "landing.html").read_text(encoding="utf-8")

@router.get("/superadmin", response_class=HTMLResponse)
async def superadmin_page():
    # HTML moved to html/internal/ per Mesio-internal namespace separation.
    p = STATIC / "html" / "internal" / "superadmin.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>", status_code=404)

@router.get("/internal", response_class=HTMLResponse)
async def internal_hq_landing():
    """Mesio HQ landing page — morning ritual dashboard for the founder."""
    p = STATIC / "html" / "internal" / "index.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>", status_code=404)

@router.get("/internal/superadmin", response_class=HTMLResponse)
async def superadmin_internal_alias():
    """Canonical URL for superadmin page — matches /api/internal/* namespace."""
    p = STATIC / "html" / "internal" / "superadmin.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>", status_code=404)

@router.get("/staff", response_class=HTMLResponse)
async def staff_app_page():
    """Unified Staff App shell — one page for every operational role.

    Replaces the old one-HTML-per-role pages (/waiter, /cashier, /kitchen,
    /bar, /courier, /staff-hq / /staff-clock), which are removed with no
    redirects (product decision, 2026-09-14). Served unconditionally, same
    as every operational page before it — the auth guard runs client-side
    in app/static/js/staff/staff-shell.js (localStorage token check), and
    the real, server-enforced session gate is GET /api/staff/sections
    (app/routes/auth_routes.py::staff_visible_sections), which the shell
    calls on mount to decide which sections to show.
    """
    return (STATIC / "html" / "staff.html").read_text(encoding="utf-8")

@router.get("/crm", response_class=HTMLResponse)
async def crm_page():
    # HTML moved to html/internal/ per Mesio-internal namespace separation.
    p = STATIC / "html" / "internal" / "crm.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>", status_code=404)

@router.get("/internal/crm", response_class=HTMLResponse)
async def crm_internal_alias():
    """Canonical URL for CRM — matches /api/internal/* namespace."""
    p = STATIC / "html" / "internal" / "crm.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>", status_code=404)


@router.get("/chat/{table_id}", response_class=HTMLResponse)
async def diner_chat_page(table_id: str):
    """Diner-facing chat surface — opened via the QR code at the table.

    Mirrors the existing /menu/{table_id} pattern (app/routes/tables.py):
    the HTML is served verbatim and table_id is read client-side from the
    URL path (see diner-session.js::dinerGetTableToken). The bot presents
    the carta and takes the order INSIDE the conversation via the
    /api/diner/* endpoints (app/routes/diner.py, blocks protocol) — this
    is NOT a separate menu-browsing page.
    """
    p = STATIC / "html" / "diner-chat.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Chat no disponible</h1>", status_code=404)

@router.get("/pedir/{slug}", response_class=HTMLResponse)
async def diner_delivery_entry_page(slug: str):
    """Public delivery/pickup ordering entry point (docs/claude/delivery-web.md
    chunk 5), ONE public link per organization. Serves the EXACT SAME
    diner-chat.html/diner-chat.js as /chat/{table_id} — the client-side code
    tells the two entry points apart from the URL path (see
    diner-session.js::dinerGetEntryMode) and runs a different bootstrap
    (GPS + GET /api/diner/org/{slug} + POST /api/diner/order-mode/resolve
    before ever opening a session) instead of forking a second chat page.

    Unlike /chat/{table_id} (table_id is opaque and never validated
    server-side before the page renders — the QR itself is the proof of
    physical presence), THIS is a link the restaurant hands out or publishes,
    so an unknown slug 404s here instead of silently rendering a broken page
    that will only fail once the frontend calls the API.
    """
    org = await delivery_repo.db_get_org_by_slug(slug.strip())
    if not org:
        return HTMLResponse("<h1>Restaurante no encontrado</h1>", status_code=404)
    p = STATIC / "html" / "diner-chat.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Chat no disponible</h1>", status_code=404)

@router.get("/pedido/{public_code}", response_class=HTMLResponse)
async def diner_delivery_status_page(public_code: str):
    """Public customer status page (docs/claude/delivery-web.md chunk 6),
    `/pedido/{public_code}`. Same "unknown -> 404 here" posture as
    /pedir/{slug} above: the public_code is a real secret a customer either
    typed correctly or followed from a link/email, not a QR scan whose mere
    existence proves anything — so an unknown code 404s at the PAGE level
    too, not just from the API the page will go on to call.

    Deliberately does NOT enter tenant_scope/bypass_tenant_scope here: this
    route only needs to know "does ANY order have this code", which
    db_get_order_by_public_code already resolves entirely on its own
    (pre-tenant, bypass_tenant_scope internally) — see that function's
    docstring for why the code space is GLOBAL, not per-org.
    """
    order = await delivery_repo.db_get_order_by_public_code(public_code.strip())
    if not order:
        return HTMLResponse("<h1>Pedido no encontrado</h1>", status_code=404)
    p = STATIC / "html" / "pedido.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Página no disponible</h1>", status_code=404)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return (STATIC / "html" / "privacy.html").read_text(encoding="utf-8")

@router.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return (STATIC / "html" / "terms.html").read_text(encoding="utf-8")

@router.get("/billing", response_class=HTMLResponse)
async def billing_page():
    p = STATIC / "html" / "billing.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Billing no disponible</h1>")

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    p = STATIC / "html" / "settings.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Settings no disponible</h1>")

@router.get("/floorplan", response_class=HTMLResponse)
async def floorplan_page():
    p = STATIC / "html" / "floorplan.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Floorplan no disponible</h1>", status_code=404)

@router.get("/team", response_class=HTMLResponse)
async def team_page():
    p = STATIC / "html" / "team.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Equipo no disponible</h1>", status_code=404)

@router.get("/orders", response_class=HTMLResponse)
async def orders_page():
    p = STATIC / "html" / "orders.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Pedidos no disponible</h1>", status_code=404)

@router.get("/reservations", response_class=HTMLResponse)
async def reservations_page():
    p = STATIC / "html" / "reservations.html"
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

@router.get("/loyalty", response_class=HTMLResponse)
async def loyalty_page():
    p = STATIC / "html" / "loyalty.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Fidelización no disponible</h1>", status_code=404)

@router.get("/customers-at-risk", response_class=HTMLResponse)
async def customers_at_risk_page():
    p = STATIC / "html" / "customers-at-risk.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Clientes en riesgo no disponible</h1>", status_code=404)

@router.get("/payroll", response_class=HTMLResponse)
async def payroll_page():
    p = STATIC / "html" / "payroll.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Nómina no disponible</h1>", status_code=404)

@router.get("/locations", response_class=HTMLResponse)
async def locations_page():
    p = STATIC / "html" / "locations.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Sucursales no disponible</h1>", status_code=404)


# ── PUBLIC APIs ───────────────────────────────────────────────────────

@router.get("/api/public/restaurant-info")
async def public_restaurant_info(id: int):
    """Return the restaurant name for a given restaurant ID (public, read-only).

    `id` here is the org_id — this endpoint is only ever called from
    login.html's `?r=` kiosk/login param, which is always an org id (see
    static/js/staff/sections/myshift.js's kiosk bootstrap comment — ported
    verbatim from the old staff-clock.js).
    """
    restaurant = await db.db_get_restaurant_by_org_id(id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    return {"name": restaurant.get("name", "")}


# ── QR-Phone-Claim (Layer 1 of table identification) ─────────────────
# Full design: docs/MESA_QR_ARCHITECTURE.md
# The customer scans a QR → /menu/{table_id} asks for their phone → the browser
# calls this endpoint to register the pre-binding (phone, table). When
# the customer sends their first message to the bot, detect_table_context looks
# up by exact phone match and opens the session on the correct table —
# no race conditions, no visible markers in WhatsApp.


@router.get("/api/geocode")
async def geocode_endpoint(request: Request, address: str):
    """
    Geocode proxy to Nominatim. Requires authentication (admin/staff Bearer token).
    Rate limit: 10 req/min per IP.
    """
    from app.routes.deps import require_auth
    await require_auth(request)

    # ── Rate limit: 10 req/min per IP ────────────────────────────────────────────
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
    Reverse geocode proxy to Nominatim. Requires authentication (admin/staff Bearer token).
    Rate limit: 10 req/min per IP (shared with /api/geocode).
    The frontend must use this endpoint instead of calling Nominatim directly.
    TODO (next wave): migrate dashboard-features.js to use this endpoint.
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
    Humans are redirected to the org's ordering page, /pedir/{slug}. (It
    sent them to the WhatsApp-era /menu/{bot_number} catalog, and to itself
    — an endless refresh — when the org had no number.)
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
    redirect_to = f"/pedir/{urllib.parse.quote(slug)}"
    esc_name    = _html.escape(name)
    description = _html.escape(f"Menú de {name} — pide en línea")

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
    Server-rendered dish page with per-dish Open Graph tags and an order CTA.
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

    # Ordering happens on the org's own web link (delivery / pickup).
    order_url = f"/pedir/{urllib.parse.quote(slug)}"

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
    page = page.replace("{{ORDER_URL}}", _html.escape(order_url))
    page = page.replace("{{MENU_URL}}", menu_url)

    return HTMLResponse(content=page, status_code=200)


@router.get("/sitemap-{restaurant_id}.xml")
async def restaurant_sitemap(restaurant_id: int):
    """
    Per-restaurant XML sitemap listing menu page + individual dish pages.
    Cached for 1 hour.

    NOTE (P0 audit 2026-09): no current caller of this route was found in
    the codebase (the SEO dish pages use slug-based URLs, not this route),
    so the intended id kind for `restaurant_id` could not be confirmed from
    a real call site. Treated as a location_id (the `restaurants` VIEW's
    own PK) — the interpretation that matches the route's pre-Wave-2 naming
    and the VIEW's `id` column. Low risk: this is a public, read-only,
    already-public-data endpoint (no auth context, no writes), so a
    genuine id collision would at worst show a different tenant's already
    public menu sitemap, not leak private data.
    """
    data = await db.db_get_restaurant_by_location_id(restaurant_id)
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
            # Brand-level page, no sede chosen yet: a dish stays listed while
            # at least one sede has it (db_get_menu_availability is per sede).
            availability = await db.db_get_menu_availability_any_sede(restaurant_id) or {}
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
