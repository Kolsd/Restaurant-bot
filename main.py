from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.chat import router

app = FastAPI(
    title="🍽️ Restaurant AI Bot",
    description="Agente de WhatsApp con IA para restaurantes",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

@app.get("/")
async def root():
    return {
        "status": "🟢 Online",
        "bot": "Restaurant AI Bot",
        "docs": "/docs",
        "endpoints": {
            "chat": "POST /api/chat",
            "twilio_webhook": "POST /api/webhook/twilio",
            "meta_webhook": "POST /api/webhook/meta",
            "reservations": "GET /api/reservations",
        }
    }
