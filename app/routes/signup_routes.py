"""
Public signup endpoint — captures prospect leads from /signup landing page.
No auth required. Rate-limited 5 attempts per 15 min per IP via state_store.
"""
import re
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel, field_validator

from app.repositories.internal import crm_repo
from app.services import state_store
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

STATIC = Path(__file__).parent.parent / "static"

_VALID_PLANS = {"Pulso", "Restaurante", "Pro", "Cadena"}
_PHONE_RE = re.compile(r"^\+?[\d\s\-]{7,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupPayload(BaseModel):
    nombre: str
    email: str
    telefono: str
    restaurante: str
    ciudad: str
    plan: str

    @field_validator("nombre", "restaurante", "ciudad")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Campo requerido")
        if len(v) > 200:
            raise ValueError("Demasiado largo")
        return v

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Email inválido")
        if len(v) > 320:
            raise ValueError("Email demasiado largo")
        return v

    @field_validator("telefono")
    @classmethod
    def valid_phone(cls, v: str) -> str:
        v = v.strip()
        if not _PHONE_RE.match(v):
            raise ValueError("Teléfono inválido")
        return v

    @field_validator("plan")
    @classmethod
    def valid_plan(cls, v: str) -> str:
        v = v.strip()
        if v not in _VALID_PLANS:
            raise ValueError(f"Plan inválido: {v}")
        return v


@router.get("/signup", response_class=HTMLResponse)
async def signup_page():
    return (STATIC / "html" / "signup.html").read_text(encoding="utf-8")


@router.post("/api/signup")
async def create_signup(payload: SignupPayload, request: Request):
    # Rate limit: 5 per 15 min per IP
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"signup:{client_ip}"
    allowed = await state_store.rate_limit_check(rate_key, max_requests=5, window_seconds=900)
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 15 minutos.")

    try:
        # Idempotency: if the same phone already has a prospect in the last hour, return silently.
        existing = await crm_repo.db_get_prospect_by_phone(payload.telefono)
        if existing:
            log.info("signup.duplicate_phone_skipped", plan=payload.plan, city=payload.ciudad)
            return {"ok": True}

        prospect = await crm_repo.db_create_prospect(
            restaurant_name=payload.restaurante,
            owner_name=payload.nombre,
            phone=payload.telefono,
            city=payload.ciudad,
            source="landing_signup",
            stage="prospecto",
            tags=[f"plan:{payload.plan.lower()}", f"email:{payload.email}"],
        )
        log.info("signup.prospect_created", prospect_id=prospect["id"], plan=payload.plan, city=payload.ciudad)
        return {"ok": True, "prospect_id": prospect["id"]}
    except Exception:
        log.exception("signup.create_prospect_failed", email=payload.email)
        raise HTTPException(status_code=500, detail="Error al registrar. Por favor intenta de nuevo.")
