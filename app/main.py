import os
import asyncio
import logging
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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
from app.routes.staff_payroll import router as staff_payroll_router
from app.routes.staff_comms import router as staff_comms_router
from app.routes.loyalty import router as loyalty_router
from app.routes.reservations import router as reservations_router
from app.routes.discounts import router as discounts_router
from app.routes.reviews import router as reviews_router
from app.routes.health import router as health_router
from app.routes.marketing import router as marketing_router
# ── Internal tools (Mesio team only — NOT restaurant-facing features) ─────────
from app.routes.internal.crm import router as internal_crm_router
from app.routes.internal.admin import router as internal_admin_router
from app.routes.internal.analytics import router as internal_analytics_router
from app.routes.internal.billing_admin import router as internal_billing_admin_router
from app.routes.internal.ops import router as internal_ops_router
from app.routes.legacy_redirects import router as legacy_redirects_router
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

    _redis_configured = bool(os.getenv("REDIS_URL"))
    _log.info("redis_url_configured", configured=_redis_configured)
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
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
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
            "https://unpkg.com; "
            "font-src 'self' data:; "
            "connect-src 'self' "
            "https://api.anthropic.com "
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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
app.include_router(staff_router)
app.include_router(staff_webauthn_router)
app.include_router(staff_payroll_router)
app.include_router(staff_comms_router)
app.include_router(loyalty_router)
app.include_router(reservations_router)
app.include_router(discounts_router)
app.include_router(reviews_router)
app.include_router(health_router)
app.include_router(marketing_router)
# ── Internal tools (Mesio team only — NOT restaurant-facing features) ─────────
app.include_router(internal_crm_router)
app.include_router(internal_admin_router)
app.include_router(internal_analytics_router)
app.include_router(internal_billing_admin_router)
app.include_router(internal_ops_router)
# ── Legacy URL redirects (30-day grace period, then remove) ───────────────────
app.include_router(legacy_redirects_router)