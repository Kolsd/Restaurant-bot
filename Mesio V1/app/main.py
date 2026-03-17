from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.routes.chat import router as chat_router
from app.routes.orders import router as orders_router
from app.routes.dashboard import router as dashboard_router
from app.routes.stats import router as stats_router
from app.routes.tables import router as tables_router
from app.services.database import init_db
from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse

app = FastAPI(
    title="🍽️ Mesio",
    description="IA colombiana para restaurantes",
    version="5.5.0"
)
@app.middleware("http")
async def force_domain_middleware(request: Request, call_next):
    host = request.headers.get("host", "")
    # Si detecta el dominio de Railway, fuerza la redirección al dominio oficial
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
    await init_db()
    from app.services.database import db_init_tables
    await db_init_tables()
    print("🚀 Mesio v5.5 iniciado")

app.include_router(dashboard_router)
app.include_router(stats_router)
app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(tables_router)
