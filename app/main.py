from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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
    version="5.7.0"
)

@app.middleware("http")
async def force_domain_middleware(request: Request, call_next):
    host = request.headers.get("host", "")
    if "railway.app" in host:
        url = str(request.url).replace(host, "mesioai.com").replace("http://", "https://")
        return RedirectResponse(url, status_code=301)
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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

    print("🚀 Mesio v5.7 iniciado", flush=True)

app.include_router(dashboard_router)
app.include_router(stats_router)
app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(tables_router)