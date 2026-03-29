import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pathlib import Path
from starlette.responses import RedirectResponse
from app.routes.chat import router as chat_router
from app.routes.orders_routes import router as orders_router
from app.routes.dashboard import router as dashboard_router
from app.routes.stats import router as stats_router
from app.routes.tables import router as tables_router
from app.routes.billing import router as billing_router
from app.routes.crm import router as crm_router
from app.routes import nps, inventory
from app.routes.sync import router as sync_router
from app.routes.staff import router as staff_router
from app.routes.loyalty import router as loyalty_router
from app.services import database as db  # ← FIX: import directo de db

APP_DOMAIN = os.getenv("APP_DOMAIN", "")

app = FastAPI(
    title="Mesio",
    description="AI assistant for restaurants",
    version="6.1.0",
    docs_url=None,
    redoc_url=None,
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


@app.on_event("startup")
async def startup():
    # Warm up the connection pool.
    # All schema migrations are handled by Alembic (run `alembic upgrade head`
    # before deploying). Do NOT add DDL calls here — with 4 uvicorn workers,
    # concurrent CREATE TABLE statements cause race conditions on startup.
    await db.init_pool()

    from app.services.scheduler import start_scheduler
    await start_scheduler()

    await db.db_cleanup_expired_sessions()

    print("Mesio v6.0 started", flush=True)


app.include_router(dashboard_router)
app.include_router(stats_router)
app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(tables_router)
app.include_router(billing_router)
app.include_router(crm_router)
app.include_router(nps.router)
app.include_router(inventory.router)
app.include_router(sync_router, prefix="/api")
app.include_router(staff_router)
app.include_router(loyalty_router)