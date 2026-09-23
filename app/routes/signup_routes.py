"""
Public self-serve signup — a restaurant creates its own Mesio account.

Until 2026-09-23 this endpoint only filed a lead in the CRM and somebody at
Mesio had to press "convertir" for the restaurant to exist, so the landing
page's "14 días gratis, sin tarjeta" was a promise the product could not
keep on its own. It now provisions the tenant in the same request
(`services/provisioning.create_tenant`) and the CRM row becomes a record of
what happened rather than the thing that makes it happen.

The owner chooses their own password here. That is deliberate: the welcome
email is best-effort (RESEND_API_KEY may not be set), and an account whose
only way in is an email that may never arrive is not a self-serve account.

No auth. Rate-limited 5 attempts per 15 min per IP via state_store.
"""
import re
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel, field_validator

from app.repositories.internal import crm_repo
from app.services import state_store
from app.services.logging import get_logger
from app.services.provisioning import (
    DEFAULT_TRIAL_DAYS,
    ProvisioningError,
    TenantAlreadyExists,
    create_tenant,
)

log = get_logger(__name__)

router = APIRouter()

STATIC = Path(__file__).parent.parent / "static"

_VALID_PLANS = {"Pulso", "Restaurante", "Pro", "Cadena"}
_PHONE_RE = re.compile(r"^\+?[\d\s\-]{7,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Short enough that a busy owner will actually type it, long enough that it
# is not the weakest part of the account.
_MIN_PASSWORD_LEN = 8


class SignupPayload(BaseModel):
    nombre: str
    email: str
    telefono: str
    restaurante: str
    ciudad: str
    plan: str
    password: str

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

    @field_validator("password")
    @classmethod
    def valid_password(cls, v: str) -> str:
        # Not stripped: leading/trailing spaces are part of a password the
        # owner typed, and silently trimming them locks them out later.
        if len(v) < _MIN_PASSWORD_LEN:
            raise ValueError(f"La contraseña debe tener al menos {_MIN_PASSWORD_LEN} caracteres")
        if len(v) > 200:
            raise ValueError("Contraseña demasiado larga")
        return v


@router.get("/signup", response_class=HTMLResponse)
async def signup_page():
    return (STATIC / "html" / "signup.html").read_text(encoding="utf-8")


async def _record_prospect(payload: SignupPayload, org_id: int, username: str) -> None:
    """File the signup in the CRM, already closed. Best-effort.

    The funnel still wants to see who signed up and on which plan, but the
    account exists either way — a CRM failure must never undo a successful
    registration, so every error here is logged and swallowed.
    """
    try:
        prospect = await crm_repo.db_create_prospect(
            restaurant_name=payload.restaurante,
            owner_name=payload.nombre,
            phone=payload.telefono,
            city=payload.ciudad,
            source="self_serve_signup",
            stage="cerrado",
            tags=[
                f"plan:{payload.plan.lower()}",
                f"email:{payload.email}",
                f"org:{org_id}",
            ],
        )
        await crm_repo.db_create_prospect_note(
            prospect["id"],
            author="system",
            content=(
                f"Alta por autoservicio → org #{org_id} ({payload.restaurante}). "
                f"Usuario: {username}. Plan elegido: {payload.plan}."
            ),
            note_type="note",
        )
    except Exception:
        log.exception("signup.crm_record_failed", org_id=org_id)


@router.post("/api/signup")
async def create_signup(payload: SignupPayload, request: Request):
    # Rate limit: 5 per 15 min per IP
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"signup:{client_ip}"
    allowed = await state_store.rate_limit_check(rate_key, max_requests=5, window_seconds=900)
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 15 minutos.")

    try:
        tenant = await create_tenant(
            restaurant_name=payload.restaurante,
            # The full email is the login name: it is the one string the
            # owner cannot forget, and it cannot collide with a different
            # person the way a local part can.
            username=payload.email,
            password=payload.password,
            owner_email=payload.email,
            # No WhatsApp number. The phone is a sales contact, and writing
            # it onto organizations.whatsapp_number (UNIQUE) would stop the
            # same owner from ever registering a second restaurant.
            whatsapp_number=None,
            plan_code=payload.plan.lower(),
            trial_days=DEFAULT_TRIAL_DAYS,
            # A person who typed their own password must be able to log in
            # with the username they typed, not with a suffixed variant
            # they would have no way to guess.
            allow_username_suffix=False,
        )
    except TenantAlreadyExists as exc:
        log.info("signup.already_exists", stage=exc.stage)
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except ProvisioningError as exc:
        log.warning("signup.provisioning_failed", stage=exc.stage)
        raise HTTPException(status_code=500, detail=exc.message) from exc
    except Exception:
        log.exception("signup.unexpected_failure")
        raise HTTPException(status_code=500, detail="Error al registrar. Por favor intenta de nuevo.")

    await _record_prospect(payload, tenant.org_id, tenant.username)

    log.info(
        "signup.tenant_created",
        org_id=tenant.org_id,
        plan=payload.plan,
        city=payload.ciudad,
        welcome_sent=tenant.welcome_email_sent,
    )
    return {
        "ok": True,
        "org_id": tenant.org_id,
        "location_id": tenant.location_id,
        # Echoed so the page can show "entrá con este usuario" without
        # making the owner wait for an email that may not be configured.
        "username": tenant.username,
        "login_url": "/login",
        "trial_days": DEFAULT_TRIAL_DAYS,
        "trial_until": tenant.comp_until.isoformat() if tenant.comp_until else None,
        "welcome_email_sent": tenant.welcome_email_sent,
    }
