from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.chat import router as chat_router
from app.routes.orders import router as orders_router

app = FastAPI(
    title="🍽️ Restaurant AI Bot",
    description="Agente de WhatsApp con IA para restaurantes — con pedidos y pagos Wompi",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/api")
app.include_router(orders_router, prefix="/api")

@app.get("/")
async def root():
    return {
        "status": "🟢 Online",
        "bot": "Restaurant AI Bot v2.0",
        "docs": "/docs",
        "new_in_v2": "Pedidos domicilio/recoger + Pagos Wompi",
        "endpoints": {
            "chat": "POST /api/chat",
            "orders": "GET /api/orders",
            "cart": "GET /api/cart/{phone}",
            "wompi_webhook": "POST /api/payment/wompi-webhook",
            "payment_confirm": "GET /api/payment/confirm",
            "twilio_webhook": "POST /api/webhook/twilio",
            "reservations": "GET /api/reservations",
        }
    }
