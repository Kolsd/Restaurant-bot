from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pathlib import Path
from starlette.responses import RedirectResponse
from app.routes.chat import router as chat_router
from app.routes.orders import router as orders_router
from app.routes.dashboard import router as dashboard_router
from app.routes.stats import router as stats_router
from app.routes.tables import router as tables_router

app = FastAPI(
    title="🍽️ Mesio",
    description="IA colombiana para restaurantes",
    version="5.8.0",
    # Ocultar docs en producción (V-14)
    docs_url=None,
    redoc_url=None,
)

# ── DOMINIO REDIRECT ─────────────────────────────────────────────────
@app.middleware("http")
async def force_domain_middleware(request: Request, call_next):
    host = request.headers.get("host", "")
    if "railway.app" in host:
        url = str(request.url).replace(host, "mesioai.com").replace("http://", "https://")
        return RedirectResponse(url, status_code=301)
    return await call_next(request)

# ── SECURITY HEADERS MIDDLEWARE (V-07 parcial, V-14) ─────────────────
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

# ── CORS RESTRICTIVO (V-07) ───────────────────────────────────────────
# Solo permitir orígenes conocidos. El webhook de Meta viene de Meta,
# no del browser, por lo que no necesita CORS.
ALLOWED_ORIGINS = [
    "https://mesioai.com",
    "https://www.mesioai.com",
    # Durante desarrollo (eliminar en producción real):
    "http://localhost:3000",
    "http://localhost:8000",
]

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
    from app.services.database import init_db, db_init_tables, db_init_waiter_alerts, db_init_table_sessions
    await init_db()
    await db_init_tables()
    await db_init_waiter_alerts()
    await db_init_table_sessions()

    from app.services.scheduler import start_scheduler
    await start_scheduler()

    # Limpiar tokens expirados al arrancar (V-06)
    from app.services.database import db_cleanup_expired_sessions
    await db_cleanup_expired_sessions()

    print("🚀 Mesio v5.8 iniciado", flush=True)


app.include_router(dashboard_router)
app.include_router(stats_router)
app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(tables_router)