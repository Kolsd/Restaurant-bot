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
from app.routes.chat import router as chat_router
from app.routes.orders_routes import router as orders_router
from app.routes.dashboard import router as dashboard_router
from app.routes.auth_routes import router as auth_router
from app.routes.settings_routes import router as settings_router
from app.routes.team_routes import router as team_router
from app.routes.stats import router as stats_router
from app.routes.tables import router as tables_router
from app.routes.billing import router as billing_router
from app.routes import nps, inventory
from app.routes.sync import router as sync_router
from app.routes.staff import router as staff_router
from app.routes.staff_webauthn import router as staff_webauthn_router
from app.routes.staff_comms import router as staff_comms_router
from app.routes.loyalty import router as loyalty_router
from app.routes.reservations import router as reservations_router
from app.routes.discounts import router as discounts_router
from app.routes.reviews import router as reviews_router
from app.routes.health import router as health_router
from app.routes.marketing import router as marketing_router
from app.routes.subscription import router as subscription_router
from app.routes.billing_subscription import router as billing_subscription_router
from app.routes.demo import router as demo_router
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

    _disable_worker = os.getenv("DISABLE_EMBEDDED_WORKER", "").strip().lower() in ("1", "true", "yes")
    _inbox_stop_event: asyncio.Event | None = None
    _inbox_task: asyncio.Task | None = None

    if not _disable_worker:
        # Start the webhook inbox worker (one per uvicorn worker process).
        # FOR UPDATE SKIP LOCKED in the worker query makes concurrent workers safe.
        from app.services.inbox_worker import run_worker
        _inbox_stop_event = asyncio.Event()
        _inbox_task = asyncio.create_task(run_worker(_inbox_stop_event))
        _log.info("inbox_worker_task_created")
    else:
        _log.info("inbox_worker_disabled", reason="DISABLE_EMBEDDED_WORKER is set")

    # Warn loudly if ANTHROPIC_API_KEY is missing — the bot will fail silently
    # on every incoming WhatsApp message without it.
    if not os.getenv("ANTHROPIC_API_KEY"):
        _log.error(
            "startup.missing_critical_key",
            key="ANTHROPIC_API_KEY",
            hint="Bot will not respond to any WhatsApp message until this is set",
        )

    _redis_configured = bool(os.getenv("REDIS_URL"))
    _log.info("redis_url_configured", configured=_redis_configured)

    # CRM env vars warning
    crm_phone_id = os.getenv("CRM_PHONE_NUMBER_ID", "").strip()
    if not crm_phone_id:
        _log.warning(
            "startup.crm_phone_id_unset",
            message="CRM_PHONE_NUMBER_ID not set — inbound WA messages on the CRM support number will not be auto-captured into prospects.",
        )
    else:
        _log.info("startup.crm_phone_id_configured", phone_id_prefix=crm_phone_id[:8])

    _log.info("app.started", version="6.0")

    yield

    # ── SHUTDOWN ──────────────────────────────────────────────────────
    if _inbox_stop_event is not None:
        _inbox_stop_event.set()

    if _inbox_task is not None:
        try:
            await asyncio.wait_for(_inbox_task, timeout=10.0)
            _log.info("inbox_worker_shutdown_clean")
        except asyncio.TimeoutError:
            _log.warning("inbox_worker_shutdown_timeout", timeout_seconds=10)
        except Exception:
            _log.exception("inbox_worker_shutdown_error")

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
    # WebAuthn (publickey-credentials-*) required for biometric staff clock-in.
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), "
        "publickey-credentials-get=*, publickey-credentials-create=*"
    )
    # HSTS — only set over HTTPS to avoid breaking local dev over plain HTTP
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains; preload",
        )
    # CSP — permissive baseline; 'unsafe-inline' still required by current frontend.
    # CSP hardening progress (Sprint v11.1):
    #   - DONE: inline <script>/<style> BLOCKS extracted from mesero.html,
    #     kitchen.html, caja.html → /static/js/pages/*.js + /static/css/pages/*.css.
    #   - PENDING (blocks 'unsafe-inline' removal):
    #     1. Inline <script>/<style> blocks in 15 remaining HTML files
    #        (bar, billing, dashboard, settings, staff-hq, login, etc.)
    #     2. Inline event handlers (onclick=, etc.) — caja.html alone has 57
    #     3. Inline style="" attributes — caja.html alone has 201
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
            "https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net "
            "https://unpkg.com "
            "https://fonts.googleapis.com; "
            "font-src 'self' data: "
            "https://fonts.gstatic.com; "
            "connect-src 'self' "
            "https://graph.facebook.com "
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
      - Images (.png/.jpg/.svg/.webp/.ico/.gif): 7 days. Rarely change.
      - JS/CSS: 1 day. Browser revalidates with If-Modified-Since (304 if same).
      - sw.js: no-cache + must-revalidate. Stale service workers are a footgun.
      - Everything else: 1 hour conservative.

    StaticFiles already emits Last-Modified, so 304 conditional GETs work for
    free. This adds explicit max-age so browsers don't heuristically guess.
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
                response.headers["Cache-Control"] = "public, max-age=86400, must-revalidate"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response


app.mount("/static", _CachedStaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Feature gating (MVP scope: bot + operational flows) ──────────────────────
# DISABLED_MODULES env var is a comma-separated list of module keys to skip.
# Default is empty — all revenue-bearing modules are ON. Per-plan enforcement
# comes from plan_limits (db_check_caps in agent.py), not from this gate.
# To disable specific modules, set: DISABLED_MODULES="loyalty,staff_webauthn"
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
app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(tables_router)
app.include_router(billing_router)
app.include_router(nps.router)
app.include_router(inventory.router)
app.include_router(sync_router, prefix="/api")
app.include_router(reservations_router)
app.include_router(health_router)
app.include_router(subscription_router)
app.include_router(billing_subscription_router)
app.include_router(demo_router)
app.include_router(signup_router)

# Feature-gated (disabled by default for MVP bot scope)
_maybe_include("staff", staff_router)
_maybe_include("staff_webauthn", staff_webauthn_router)
_maybe_include("staff_comms", staff_comms_router)
_maybe_include("loyalty", loyalty_router)
_maybe_include("discounts", discounts_router)
_maybe_include("reviews", reviews_router)
_maybe_include("marketing", marketing_router)
# ── Internal tools (Mesio team only — NOT restaurant-facing features) ─────────
app.include_router(internal_crm_router)
app.include_router(internal_admin_router)
app.include_router(internal_analytics_router)
app.include_router(internal_billing_admin_router)
app.include_router(internal_ops_router)
app.include_router(internal_costs_router)
app.include_router(internal_search_router)
app.include_router(internal_notifications_router)

# ── Audit middleware (HQ compliance) ─────────────────────────────────────────
# Records every state-changing /api/internal/* call to hq_audit_log.
# Registered AFTER routers so it wraps all routes. Best-effort: errors in
# audit writing never break the request that triggered the write.
from app.services.audit_middleware import AuditMiddleware  # noqa: E402
app.add_middleware(AuditMiddleware)