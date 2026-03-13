from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.chat import router as chat_router
from app.routes.orders import router as orders_router
from app.routes.dashboard import router as dashboard_router

app = FastAPI(
    title="🍽️ Restaurant AI Bot",
    description="Agente de WhatsApp con IA para restaurantes — con pedidos, pagos y dashboard",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard_router)
app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
