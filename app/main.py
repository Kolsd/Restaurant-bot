import os
import asyncio
import logging
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pathlib import Path
from starlette.responses import RedirectResponse

logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
)

structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer() # Formato amigable para Railway/Terminal
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

from pathlib import Path
from starlette.responses import RedirectResponse
from app.routes.orders_routes import router as orders_router
from app.routes.dashboard import router as dashboard_router
from app.routes.live_demo import router as live_demo_router
from app.routes.auth_routes import router as auth_router
from app.routes.settings_routes import router as settings_router
from app.routes.sede_menu_routes import router as sede_menu_router
from app.routes.menu_import_routes import router as menu_import_router
from app.routes.onboarding_routes import router as onboarding_router
from app.routes.team_routes import router as team_router
from app.routes.stats import router as stats_router
from app.routes.tables import router as tables_router
from app.routes.diner import router as diner_router
from app.routes.diner_delivery import router as diner_delivery_router
from app.routes.location_delivery import router as location_delivery_router
from app.routes.billing import router as billing_router
from app.routes import nps, inventory
from app.routes.sync import router as sync_router
from app.routes.staff import router as staff_router
from app.routes.staff_delivery import router as staff_delivery_router
from app.routes.reservations import router as reservations_router
from app.routes.health import router as health_router
from app.routes.subscription import router as subscription_router
from app.routes.billing_subscription import router as billing_subscription_router
from app.routes.signup_routes import router as signup_router
# ── Internal tools (Mesio team only — NOT restaurant-facing features) ─────────
from app.routes.internal.crm import router as internal_crm_router
from app.routes.internal.admin import router as internal_admin_router
from app.routes.internal.analytics import router as internal_analytics_router
from app.routes.internal.billing_admin import router as internal_billing_admin_router
from app.routes.internal.ops import router as internal_ops_router
from app.routes.internal.costs import router as internal_costs_router
from app.routes.internal.search import router as internal_search_router
from app.routes.internal.notifications import router as internal_notifications_router
from app.services import database as db  # ← FIX: import directo de db
from app.services.logging import get_logger as _get_logger

_log = _get_logger(__name__)

APP_DOMAIN = os.getenv("APP_DOMAIN", "")


@asynccontextmanager
async def lifespan(app):
    # ── STARTUP ───────────────────────────────────────────────────────
    # All schema migrations are handled by Alembic (run `alembic upgrade head`
    # before deploying). Do NOT add DDL calls here — with 4 uvicorn workers,
    # concurrent CREATE TABLE statements cause race conditions on startup.
    from app.services.sentry import init_sentry
    init_sentry("web")

    await db.init_pool()

    from app.services.scheduler import start_scheduler
    await start_scheduler()

    await db.db_cleanup_expired_sessions()

    # Warn loudly if ANTHROPIC_API_KEY is missing — free text typed in the
    # diner chat gets no answer without it.
    if not os.getenv("ANTHROPIC_API_KEY"):
        _log.error(
            "startup.missing_critical_key",
            key="ANTHROPIC_API_KEY",
            hint="The diner chat cannot answer free text until this is set",
        )

    _redis_configured = bool(os.getenv("REDIS_URL"))
    _log.info("redis_url_configured", configured=_redis_configured)

    # Turnstile (delivery/pickup diner entry, docs/claude/delivery-web.md) —
    # logged ONCE here, never per request (app/services/turnstile.py).
    from app.services.turnstile import log_startup_state as _turnstile_log_startup_state
    _turnstile_log_startup_state()

    _log.info("app.started", version="6.0")

    yield

    # ── SHUTDOWN ──────────────────────────────────────────────────────
    from app.services.realtime import shutdown as realtime_shutdown
    await realtime_shutdown()

    from app.services.redis_client import close_redis
    await close_redis()


app = FastAPI(
    title="Mesio",
    description="AI assistant for restaurants",
    version="6.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ── DOMINIO REDIRECT ─────────────────────────────────────────────────
@app.middleware("http")
async def force_domain_middleware(request: Request, call_next):
    host = request.headers.get("host", "")
    if APP_DOMAIN and "railway.app" in host:
        url = str(request.url).replace(host, APP_DOMAIN).replace("http://", "https://")
        return RedirectResponse(url, status_code=301)
    return await call_next(request)

# ── SECURITY HEADERS MIDDLEWARE ───────────────────────────────────────
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # X-XSS-Protection deprecated (and harmful on legacy browsers in 'mode=block').
    # Modern browsers ignore this header; explicitly disable to avoid edge-case bugs.
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HTML pages are returned by route handlers that read the file and set no
    # Cache-Control at all, which leaves the browser free to invent one from
    # Last-Modified (commonly ~10% of the file's age). An old page can then go
    # on loading old script tags after a deploy. `setdefault` so a handler
    # that deliberately set its own — and the static mount below, which runs
    # before this middleware — keeps it.
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-cache")
    # geolocation=(self) — NOT (): the delivery/pickup ordering page
    # (docs/claude/delivery-web.md chunk 5, /pedir/{slug}) calls
    # navigator.geolocation.getCurrentPosition() from OUR OWN origin to
    # resolve the sede. An empty allowlist blocks that call in every browser
    # that enforces Permissions-Policy (Chrome/Edge) with no visible error —
    # the geolocation prompt just never appears — silently breaking the
    # entire entry flow. `self` still denies every third-party/iframe embed.
    response.headers["Permissions-Policy"] = (
        "geolocation=(self), microphone=(), camera=(), "
        "publickey-credentials-get=(), publickey-credentials-create=()"
    )
    # HSTS — only set over HTTPS to avoid breaking local dev over plain HTTP
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains; preload",
        )
    # CSP — permissive baseline; 'unsafe-inline' still required by current frontend.
    # CSP hardening progress (Sprint v11.1):
    #   - DONE: inline <script>/<style> BLOCKS extracted from waiter.html,
    #     kitchen.html, cashier.html → /static/js/pages/*.js + /static/css/pages/*.css.
    #   - PENDING (blocks 'unsafe-inline' removal):
    #     1. Inline <script>/<style> blocks in 15 remaining HTML files
    #        (bar, billing, dashboard, settings, staff-hq, login, etc.)
    #     2. Inline event handlers (onclick=, etc.) — cashier.html alone has 57
    #     3. Inline style="" attributes — cashier.html alone has 201
    #   Until those three categories are migrated, dropping 'unsafe-inline'
    #   would break every dashboard page. Tracked as a follow-up sprint.
    # CDN allowlist rationale:
    #   script-src   cdnjs + jsdelivr + unpkg  → Chart.js, qrcodejs, Leaflet
    #   style-src    jsdelivr + unpkg          → Leaflet CSS (only external stylesheet today)
    #   connect-src  cdnjs + jsdelivr + unpkg  → lib .js.map fetches in devtools
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "img-src 'self' https: data:; "
            "script-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net "
            "https://unpkg.com "
            "https://cdnjs.cloudflare.com "
            "https://challenges.cloudflare.com; "
            "frame-src 'self' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net "
            "https://unpkg.com "
            "https://fonts.googleapis.com; "
            "font-src 'self' data: "
            "https://fonts.gstatic.com; "
            "connect-src 'self' "
            "https://checkout.wompi.co "
            "https://nominatim.openstreetmap.org "
            "https://res.cloudinary.com "
            "https://api.cloudinary.com "
            "https://cdn.jsdelivr.net "
            "https://unpkg.com "
            "https://cdnjs.cloudflare.com; "
            "frame-ancestors 'none'"
        ),
    )
    return response

# ── CORS ──────────────────────────────────────────────────────────────
_origins_env = os.getenv("APP_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()] or [
    "http://localhost:3000",
    "http://localhost:8000",
]
if APP_DOMAIN:
    ALLOWED_ORIGINS += [f"https://{APP_DOMAIN}", f"https://www.{APP_DOMAIN}"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Branch-ID", "X-Location-ID"],
)

# Gzip-compress all responses ≥ 500 bytes. Browsers send Accept-Encoding: gzip
# by default, so this is transparent. Saves 70-80% on JS/CSS/HTML/JSON wire size.
# minimum_size=500 avoids overhead for tiny responses (status, errors, etc.).
app.add_middleware(GZipMiddleware, minimum_size=500)

STATIC_DIR = Path(__file__).parent / "static"


class _CachedStaticFiles(StaticFiles):
    """StaticFiles with Cache-Control headers tuned per file type.

    Strategy:
      - Images (.png/.jpg/.svg/.webp/.ico/.gif): 7 days. Rarely change, and
        a stale logo is not a broken app.
      - JS/CSS: `no-cache` — cacheable, but revalidated on every use.
      - sw.js: no-cache + must-revalidate. Stale service workers are a footgun.
      - Everything else: 1 hour conservative.

    **Why JS/CSS are not cached for a day.** They used to carry
    `max-age=86400, must-revalidate`, which reads like "revalidate" but does
    not: `must-revalidate` only governs what happens once a response is
    STALE, so for 24 hours the browser served its copy without ever asking.
    A deploy therefore reached a tablet whenever its day happened to end —
    observed in this very app on 2026-09-23, where a fixed script kept
    running in its broken version until the cache was forced. `no-cache`
    keeps the file in the cache and makes the browser revalidate before
    using it: unchanged files come back as a 304 with no body, so the cost
    is one conditional request per asset, and a deploy is live immediately.

    The right end state is content-hashed URLs (`app.a1b2c3.js`, cached for
    a year), which needs the script tags to be generated rather than
    hand-written in 30 HTML files. Until then, correctness beats the
    handful of 304s.
    """

    _IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico", ".gif")
    _ASSET_EXTS = (".js", ".css", ".woff", ".woff2", ".ttf")

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            lower = path.lower()
            if lower.endswith("sw.js") or lower.endswith("/sw.js"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            elif lower.endswith(self._IMAGE_EXTS):
                response.headers["Cache-Control"] = "public, max-age=604800"
            elif lower.endswith(self._ASSET_EXTS):
                response.headers["Cache-Control"] = "public, no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response


app.mount("/static", _CachedStaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Feature gating (MVP scope: bot + operational flows) ──────────────────────
# DISABLED_MODULES env var is a comma-separated list of module keys to skip.
# Default is empty — all revenue-bearing modules are ON. Per-plan enforcement
# comes from plan_limits (db_check_caps in agent.py), not from this gate.
# To disable specific modules, set: DISABLED_MODULES="reservations"
_DEFAULT_DISABLED = ""
_disabled_modules = {
    m.strip()
    for m in os.getenv("DISABLED_MODULES", _DEFAULT_DISABLED).split(",")
    if m.strip()
}

def _maybe_include(key: str, router, **kwargs) -> None:
    if key in _disabled_modules:
        _log.info("router.disabled", module=key)
        return
    app.include_router(router, **kwargs)

# Always-on (bot + operational core)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(team_router)
app.include_router(stats_router)
app.include_router(sede_menu_router)
app.include_router(menu_import_router)
app.include_router(onboarding_router)
app.include_router(orders_router, prefix="/api")
app.include_router(tables_router)
app.include_router(diner_router)
app.include_router(diner_delivery_router)
app.include_router(location_delivery_router)
app.include_router(billing_router)
app.include_router(nps.router)
app.include_router(inventory.router)
app.include_router(sync_router, prefix="/api")
app.include_router(reservations_router)
app.include_router(health_router)
app.include_router(subscription_router)
app.include_router(billing_subscription_router)
app.include_router(signup_router)

# Feature-gated (disabled by default for MVP bot scope)
_maybe_include("staff", staff_router)
_maybe_include("staff_delivery", staff_delivery_router)
# ── Internal tools (Mesio team only — NOT restaurant-facing features) ─────────
app.include_router(internal_crm_router)
app.include_router(internal_admin_router)
app.include_router(internal_analytics_router)
app.include_router(internal_billing_admin_router)
app.include_router(internal_ops_router)
app.include_router(internal_costs_router)
app.include_router(internal_search_router)
app.include_router(internal_notifications_router)
app.include_router(live_demo_router)

# ── Audit middleware (HQ compliance) ─────────────────────────────────────────
# Records every state-changing /api/internal/* call to hq_audit_log.
# Registered AFTER routers so it wraps all routes. Best-effort: errors in
# audit writing never break the request that triggered the write.
from app.services.audit_middleware import AuditMiddleware  # noqa: E402
app.add_middleware(AuditMiddleware)