"""
app/routes/internal/admin.py

Superadmin CRUD endpoints for Mesio internal use only.
These are NOT restaurant-facing features — they are tools for the Mesio team.

Endpoints (all under /api/internal/admin, all require verify_superadmin):
  POST /login            → Exchange ADMIN_KEY for session token
  POST /logout           → Invalidate superadmin session
  GET  /stats            → Platform aggregate stats
  GET  /restaurants      → All restaurants list (legacy)
  POST /create-user      → Create a user tied to a restaurant
  POST /delete-user      → Delete a user
  GET  /users            → List all users
  POST /create-restaurant → Create a new restaurant (legacy)
  POST /set-subscription → Set subscription status (legacy)
  GET  /restaurant/{id}  → Detail + stats for one restaurant (legacy)
  POST /update-restaurant → Update restaurant fields (legacy)
  GET  /billing-stats    → Billing aggregate stats
  POST /fix-branch-ids   → Fix branch IDs (maintenance tool)
  POST /fix-conversations → Fix conversation bot numbers (maintenance tool)
  POST /parse-menu       → Parse PDF/image into JSON menu via Claude

  --- Organization + Location (new model) ---
  GET    /organizations              → List all orgs with location_count
  POST   /organizations              → Create org + primary location atomically
  GET    /organizations/{id}         → Org detail + locations + stats
  PATCH  /organizations/{id}         → Update org fields
  DELETE /organizations/{id}         → Soft-delete (or hard if ?hard=true + no recent orders)
  GET    /organizations/{id}/locations → List locations for an org
  POST   /organizations/{id}/locations → Create a location under an org
  PATCH  /locations/{id}             → Update location fields (is_primary ignored, vestigial)
  DELETE /locations/{id}             → Soft/hard delete
"""
import os
import io
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, File, UploadFile, Depends, Query
from pydantic import BaseModel, field_validator
from anthropic import Anthropic

from app.services.auth import create_user, get_users, hash_password
from app.services import database as db
from app.routes.deps import verify_superadmin
from app.repositories import sessions_repo, restaurant_repo
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.security import compare_secret
from app.services.state_store import rate_limit_check
from app.services.password_hash import hash_password as hash_pw

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal/admin", tags=["internal-admin"])


# ── Response helpers ─────────────────────────────────────────────────────────

def _ok(data) -> dict:
    return {"success": True, "data": data}


def _err(msg: str, code: int = 400) -> dict:
    return {"success": False, "error": msg, "code": code}


# ── Pydantic models (Org + Location) ────────────────────────────────────────

_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,19}$")


class CreateOrgRequest(BaseModel):
    name: str
    slug: Optional[str] = None
    whatsapp_number: Optional[str] = None
    wa_phone_id: Optional[str] = None
    wa_access_token: Optional[str] = None
    features: Optional[dict] = None
    subscription_plan: Optional[str] = "free"

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name cannot be empty")
        return v.strip()

    @field_validator("whatsapp_number")
    @classmethod
    def phone_format(cls, v):
        if v and not _PHONE_RE.match(v.strip()):
            raise ValueError("whatsapp_number format invalid")
        return v.strip() if v else None


class PatchOrgRequest(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    whatsapp_number: Optional[str] = None
    wa_phone_id: Optional[str] = None
    wa_access_token: Optional[str] = None
    features: Optional[dict] = None
    subscription_plan: Optional[str] = None
    subscription_status: Optional[str] = None

    @field_validator("whatsapp_number")
    @classmethod
    def phone_format(cls, v):
        if v and not _PHONE_RE.match(v.strip()):
            raise ValueError("whatsapp_number format invalid")
        return v.strip() if v else None


class CreateLocationRequest(BaseModel):
    name: str
    code: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    whatsapp_number: Optional[str] = None
    wa_phone_id: Optional[str] = None
    wa_access_token: Optional[str] = None
    active: Optional[bool] = True
    timezone: Optional[str] = "America/Bogota"

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name cannot be empty")
        return v.strip()

    @field_validator("latitude")
    @classmethod
    def lat_range(cls, v):
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("latitude must be in [-90, 90]")
        return v

    @field_validator("longitude")
    @classmethod
    def lon_range(cls, v):
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("longitude must be in [-180, 180]")
        return v

    @field_validator("whatsapp_number")
    @classmethod
    def phone_format(cls, v):
        if v and not _PHONE_RE.match(v.strip()):
            raise ValueError("whatsapp_number format invalid")
        return v.strip() if v else None


class PatchLocationRequest(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    whatsapp_number: Optional[str] = None
    wa_phone_id: Optional[str] = None
    wa_access_token: Optional[str] = None
    active: Optional[bool] = None
    timezone: Optional[str] = None
    is_primary: Optional[bool] = None

    @field_validator("latitude")
    @classmethod
    def lat_range(cls, v):
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("latitude must be in [-90, 90]")
        return v

    @field_validator("longitude")
    @classmethod
    def lon_range(cls, v):
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("longitude must be in [-180, 180]")
        return v

    @field_validator("whatsapp_number")
    @classmethod
    def phone_format(cls, v):
        if v and not _PHONE_RE.match(v.strip()):
            raise ValueError("whatsapp_number format invalid")
        return v.strip() if v else None


async def _bypass_internal_admin():
    """FastAPI dependency: enter bypass_tenant_scope for all internal admin routes.

    Internal admin routes are cross-tenant by design — they enumerate all
    restaurants and users.  This bypass allows migrated repos to execute
    without a pinned tenant_scope.
    """
    with bypass_tenant_scope("internal_admin_cross_tenant: superadmin operations spanning all tenants; auth verified via session token"):
        yield


# ── Pydantic models ──────────────────────────────────────────────────────────
class AdminLoginRequest(BaseModel): key: str
class CreateUserRequest(BaseModel): username: str; password: str; restaurant_id: int; admin_key: str = ""
class CreateRestaurantRequest(BaseModel): admin_key: str = ""; name: str; whatsapp_number: str; address: str; menu: str; features: dict = {}; wa_phone_id: str = ""; wa_access_token: str = ""
class SetSubscriptionRequest(BaseModel): admin_key: str = ""; restaurant_id: int; status: str
class UpdateRestaurantRequest(BaseModel):
    admin_key: str = ""; restaurant_id: int
    name: str = None; address: str = None; whatsapp_number: str = None
    wa_phone_id: str = None; wa_access_token: str = None
    features: dict = None; menu: str = None


_VALID_PLANS = {"pulso", "restaurante", "pro", "cadena", "comp", "free"}


class ChangePlanRequest(BaseModel):
    plan_code: str
    comp_until: Optional[str] = None  # ISO date "YYYY-MM-DD", only meaningful when plan_code == "comp"

    @field_validator("plan_code")
    @classmethod
    def plan_valid(cls, v: str) -> str:
        if v not in _VALID_PLANS:
            raise ValueError(f"plan_code must be one of {sorted(_VALID_PLANS)}")
        return v


class SetCompRequest(BaseModel):
    comp_until: Optional[str] = None  # ISO date "YYYY-MM-DD" or null to clear


class ResetPasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def pw_min_len(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("new_password must be at least 8 characters")
        return v


# ── SUPER ADMIN SESSION ──────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(payload: AdminLoginRequest, request: Request):
    """Exchange ADMIN_KEY for a session token. The raw key is never stored client-side."""
    ip = request.client.host if request.client else "unknown"
    ok = await rate_limit_check(f"admin_login:{ip}", max_requests=5, window_seconds=900)
    if not ok:
        raise HTTPException(status_code=429, detail="Demasiados intentos, intenta más tarde")
    if not compare_secret(payload.key, os.getenv("ADMIN_KEY", "")):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    token = await sessions_repo.create_session("superadmin")
    return {"token": token}


@router.post("/logout")
async def admin_logout_session(req: Request):
    """Invalidate the current superadmin session token."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if token:
        await sessions_repo.delete_session(token)
    return {"success": True}


@router.get("/stats")
async def admin_get_stats(
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    return await restaurant_repo.db_get_admin_stats()


@router.get("/restaurants")
async def admin_get_restaurants(
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    # Wave-2 canonical: the Mesio internal admin view enumerates CUSTOMER
    # BUSINESSES (one row per org), not "primary locations". The endpoint
    # path stays /restaurants and the response key stays "restaurants" for
    # frontend backwards compat — the shape is org-level data which is what
    # the admin actually wants (one entry per tenant). Active orgs only;
    # use active_only=False if you also need cancelled tenants.
    return {"restaurants": await db.db_get_all_orgs(active_only=False)}


@router.post("/create-user")
async def admin_create_user(
    request: CreateUserRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    success = await db.db_create_user(
        username=request.username,
        password_hash=hash_password(request.password),
        restaurant_name=rest["name"],
        role="owner",
        branch_id=request.restaurant_id
    )

    if not success:
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    return {"success": True}


@router.post("/delete-user")
async def admin_delete_user(
    username: str,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    await restaurant_repo.db_delete_user(username)
    return {"success": True}


@router.get("/users")
async def admin_list_users(
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    return {"users": await get_users()}


@router.post("/create-restaurant")
async def admin_create_restaurant(
    request: CreateRestaurantRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    from app.routes.dashboard import geocode_address
    try:
        menu_dict = json.loads(request.menu)
    except Exception:
        raise HTTPException(status_code=400, detail="Menú no es JSON válido")
    lat, lon, _ = await geocode_address(request.address)

    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict, lat, lon, request.features)

    if request.wa_access_token:
        await restaurant_repo.db_set_restaurant_wa_credentials(
            request.whatsapp_number, request.wa_phone_id, request.wa_access_token
        )

    return {"success": True}


@router.post("/set-subscription")
async def admin_set_subscription(
    request: SetSubscriptionRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True}


@router.get("/restaurant/{restaurant_id}")
async def admin_get_restaurant_detail(
    restaurant_id: int,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    rest = await db.db_get_restaurant_by_id(restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    wa = rest.get("whatsapp_number", "")
    stats = await restaurant_repo.db_get_restaurant_detail_stats(restaurant_id, wa)
    return {"restaurant": rest, "stats": stats}


@router.post("/update-restaurant")
async def admin_update_restaurant(
    request: UpdateRestaurantRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    from app.routes.dashboard import geocode_address
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    lat = lon = None
    if request.address is not None:
        lat, lon, _ = await geocode_address(request.address)

    merged_features = None
    if request.features is not None:
        raw = rest.get("features") or {}
        current = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except Exception:
                current = {}
        current.update(request.features)
        merged_features = current

    parsed_menu = None
    if request.menu is not None:
        try:
            parsed_menu = json.loads(request.menu)
        except Exception:
            raise HTTPException(status_code=400, detail="Menú no es JSON válido")

    await restaurant_repo.db_update_restaurant_fields(
        request.restaurant_id,
        name=request.name,
        address=request.address,
        latitude=lat,
        longitude=lon,
        whatsapp_number=request.whatsapp_number,
        wa_phone_id=request.wa_phone_id,
        wa_access_token=request.wa_access_token,
        features=merged_features,
        menu=parsed_menu,
    )
    return {"success": True, "restaurant": await db.db_get_restaurant_by_id(request.restaurant_id)}


@router.get("/billing-stats")
async def admin_billing_stats(
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    stats = await restaurant_repo.db_get_billing_stats()
    return {"stats": stats}


@router.post("/fix-branch-ids")
async def fix_branch_ids(
    request: Request,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    fixed = await restaurant_repo.db_fix_branch_ids()
    return {"success": True, "fixed": fixed}


@router.post("/fix-conversations")
async def fix_conversations_bot_number(
    request: Request,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    body = await request.json()
    await restaurant_repo.db_fix_conversations_bot_number(body.get("bot_number", ""))
    return {"success": True}


@router.post("/parse-menu")
async def admin_parse_menu(file: UploadFile = File(...), _: None = Depends(verify_superadmin)):
    import pypdf
    content  = await file.read()
    filename = file.filename.lower()
    client   = Anthropic()
    messages_content = []
    try:
        if filename.endswith(".pdf"):
            pdf_reader = pypdf.PdfReader(io.BytesIO(content))
            text = "".join(p.extract_text() + "\n" for p in pdf_reader.pages)
            messages_content.append({"type": "text", "text": f"Extrae el menú:\n{text}"})
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            mt = "image/png" if filename.endswith(".png") else "image/jpeg"
            messages_content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(content).decode()}})
            messages_content.append({"type": "text", "text": "Extrae el menú de esta imagen."})
        else:
            raise HTTPException(status_code=400, detail="Sube PDF, PNG o JPG")
        response = client.messages.create(
            model="claude-3-haiku-20240307", max_tokens=4000, temperature=0,
            system='Extrae menús a JSON puro: {"Categoría": [{"name":"","price":0.0,"description":""}]}',
            messages=[{"role": "user", "content": messages_content}]
        )
        return {"success": True, "json_menu": json.loads(response.content[0].text.replace("```json","").replace("```","").strip())}
    except HTTPException:
        raise
    except json.JSONDecodeError:
        log.warning("admin.parse_menu.json_decode_error")
        raise HTTPException(status_code=422, detail="El modelo no devolvió JSON válido. Intenta con otra imagen o PDF.")
    except Exception:
        log.exception("admin.parse_menu.unexpected_error")
        raise HTTPException(status_code=500, detail="Error interno al procesar el menú")

# ═══════════════════════════════════════════════════════════════════════════════
# Organizations  (new model — S7)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/organizations")
async def list_organizations(
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """List all Orgs with location_count."""
    orgs = await restaurant_repo.db_list_organizations()
    return _ok({"organizations": orgs, "total": len(orgs)})


@router.post("/organizations")
async def create_organization(
    body: CreateOrgRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Create Org + auto-create primary Location atomically.

    Returns {org, primary_location}.
    """
    import asyncpg  # noqa: PLC0415

    try:
        org = await restaurant_repo.db_create_organization(
            name=body.name,
            whatsapp_number=body.whatsapp_number,
            wa_phone_id=body.wa_phone_id,
            wa_access_token=body.wa_access_token,
            slug=body.slug,
            features=body.features or {},
            subscription_plan=body.subscription_plan or "free",
        )
    except asyncpg.UniqueViolationError as exc:
        log.warning("create_organization.conflict", detail=str(exc))
        raise HTTPException(status_code=409, detail="slug or whatsapp_number already exists")
    except Exception as exc:
        log.exception("create_organization.error", detail=str(exc))
        raise HTTPException(status_code=500, detail="Error al crear la organizacion")

    # Auto-create the primary location (named "Principal")
    try:
        primary_loc = await restaurant_repo.db_create_location(
            org_id=org["id"],
            name="Principal",
            code="principal",
            active=True,
        )
    except Exception as exc:
        log.exception("create_organization.primary_location_error", org_id=org["id"], detail=str(exc))
        raise HTTPException(status_code=500, detail="Org creada pero fallo al crear sede principal")

    log.info("create_organization.success", org_id=org["id"], org_name=org["name"])
    return _ok({"org": org, "primary_location": primary_loc})


@router.get("/organizations/{org_id}")
async def get_organization_detail(
    org_id: int,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Full org detail: org row + all locations."""
    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")

    locations = await restaurant_repo.db_get_org_locations(org_id, active_only=False)

    return _ok({
        "org": org,
        "locations": locations,
        "location_count": len(locations),
    })


@router.patch("/organizations/{org_id}")
async def update_organization(
    org_id: int,
    body: PatchOrgRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Update whitelisted org fields. Features dict is merged (not replaced)."""
    import asyncpg  # noqa: PLC0415

    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")

    updates: dict = {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        updates["name"] = name
    if body.slug is not None:
        updates["slug"] = body.slug or None
    if body.whatsapp_number is not None:
        updates["whatsapp_number"] = body.whatsapp_number or None
    if body.wa_phone_id is not None:
        updates["wa_phone_id"] = body.wa_phone_id or None
    if body.wa_access_token is not None:
        updates["wa_access_token"] = body.wa_access_token or None
    if body.subscription_plan is not None:
        updates["subscription_plan"] = body.subscription_plan
    if body.subscription_status is not None:
        updates["subscription_status"] = body.subscription_status
    if body.features is not None:
        current = org.get("features") or {}
        current.update(body.features)
        updates["features"] = current

    if not updates:
        return _ok({"org": org})

    try:
        updated = await restaurant_repo.db_update_organization(org_id, **updates)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="slug or whatsapp_number already exists")

    return _ok({"org": updated})


@router.delete("/organizations/{org_id}")
async def delete_organization(
    org_id: int,
    hard: bool = Query(default=False, description="Hard delete if true AND no recent orders"),
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Soft-delete (default) or hard-delete an Org.

    Soft: sets subscription_status='cancelled' + deactivates all locations.
    Hard: only if no orders in last 90 days and ?hard=true. Cascades via FK.
    """
    from app.services.database import get_pool  # noqa: PLC0415

    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")

    if hard:
        pool = await get_pool()
        with bypass_tenant_scope("delete_organization_hard_check"):
            async with pool.acquire() as conn:
                recent = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE org_id = $1 AND created_at > NOW() - INTERVAL '90 days'",
                    org_id,
                )
        if recent and recent > 0:
            raise HTTPException(
                status_code=409,
                detail="No se puede eliminar: existen pedidos en los ultimos 90 dias. Usa soft-delete.",
            )
        pool = await get_pool()
        with bypass_tenant_scope("delete_organization_hard"):
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
        log.info("delete_organization.hard", org_id=org_id)
        return _ok({"deleted": True, "hard": True, "org_id": org_id})

    # Soft delete
    await restaurant_repo.db_update_organization(org_id, subscription_status="cancelled")
    locations = await restaurant_repo.db_get_org_locations(org_id, active_only=False)
    for loc in locations:
        await restaurant_repo.db_update_location(loc["id"], active=False)

    log.info("delete_organization.soft", org_id=org_id, locations_deactivated=len(locations))
    return _ok({"deleted": True, "hard": False, "org_id": org_id, "locations_deactivated": len(locations)})


# ═══════════════════════════════════════════════════════════════════════════════
# Locations  (new model — S7)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/organizations/{org_id}/locations")
async def list_org_locations(
    org_id: int,
    active_only: bool = Query(default=False),
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """List all locations for a specific org."""
    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")
    locations = await restaurant_repo.db_get_org_locations(org_id, active_only=active_only)
    return _ok({"locations": locations, "org_id": org_id, "total": len(locations)})


@router.post("/organizations/{org_id}/locations")
async def create_location(
    org_id: int,
    body: CreateLocationRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Create a new (non-primary) Location under an Org."""
    import asyncpg  # noqa: PLC0415

    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")

    kwargs: dict = {}
    for field in ("code", "address", "latitude", "longitude",
                  "whatsapp_number", "wa_phone_id", "wa_access_token",
                  "active", "timezone"):
        val = getattr(body, field)
        if val is not None:
            kwargs[field] = val

    try:
        loc = await restaurant_repo.db_create_location(org_id=org_id, name=body.name, **kwargs)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="whatsapp_number already assigned to another location")
    except Exception as exc:
        log.exception("create_location.error", org_id=org_id, detail=str(exc))
        raise HTTPException(status_code=500, detail="Error al crear la sede")

    log.info("create_location.success", org_id=org_id, location_id=loc["id"])
    return _ok({"location": loc})


@router.patch("/locations/{location_id}")
async def update_location(
    location_id: int,
    body: PatchLocationRequest,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Update location fields.

    Post-Wave-2: is_primary is vestigial and will be dropped in migration 0038.
    The is_primary field in the request body is silently ignored.
    """
    import asyncpg  # noqa: PLC0415

    loc = await restaurant_repo.db_get_location_by_id(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Sede no encontrada")

    updates: dict = {}
    for field in ("name", "code", "address", "latitude", "longitude",
                  "whatsapp_number", "wa_phone_id", "wa_access_token",
                  "active", "timezone"):
        val = getattr(body, field)
        if val is not None:
            updates[field] = val

    if not updates:
        return _ok({"location": loc})

    try:
        updated = await restaurant_repo.db_update_location(location_id, **updates)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="whatsapp_number conflict")

    return _ok({"location": updated})


# ═══════════════════════════════════════════════════════════════════════════════
# Audit log  (HQ compliance — read-only endpoint)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/audit-log")
async def get_audit_log(
    actor: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    org_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    _: None = Depends(verify_superadmin),
):
    """Returns recent audit log entries. Filter by actor / target / org.

    This endpoint is intentionally NOT wrapped by _bypass_internal_admin
    (which yields inside a context manager). Instead we use bypass_tenant_scope
    directly here — hq_audit_log is a GLOBAL table with no tenant scope.
    """
    from app.repositories.internal.audit_log_repo import (  # noqa: PLC0415
        db_get_audit_log,
        db_count_audit_log,
    )

    with bypass_tenant_scope("internal_audit_log_query"):
        rows = await db_get_audit_log(
            actor=actor,
            target_type=target_type,
            target_id=target_id,
            org_id=org_id,
            limit=min(limit, 500),
            offset=offset,
        )
        total = await db_count_audit_log(actor=actor, org_id=org_id)

    return {"items": rows, "total": total}


@router.delete("/locations/{location_id}")
async def delete_location(
    location_id: int,
    hard: bool = Query(default=False, description="Hard delete if no recent orders"),
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Delete a location.

    Soft delete (default): sets active=false.
    Hard delete: only if no orders in last 90 days.
    """
    from app.services.database import get_pool  # noqa: PLC0415

    loc = await restaurant_repo.db_get_location_by_id(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Sede no encontrada")

    if hard:
        pool = await get_pool()
        with bypass_tenant_scope("delete_location_hard_check"):
            async with pool.acquire() as conn:
                recent = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE location_id = $1 AND created_at > NOW() - INTERVAL '90 days'",
                    location_id,
                )
        if recent and recent > 0:
            raise HTTPException(
                status_code=409,
                detail="No se puede eliminar: existen pedidos en los ultimos 90 dias.",
            )
        pool = await get_pool()
        with bypass_tenant_scope("delete_location_hard"):
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM locations WHERE id = $1", location_id)
        log.info("delete_location.hard", location_id=location_id)
        return _ok({"deleted": True, "hard": True, "location_id": location_id})

    updated = await restaurant_repo.db_update_location(location_id, active=False)
    log.info("delete_location.soft", location_id=location_id)
    return _ok({"deleted": True, "hard": False, "location": updated})


# ═══════════════════════════════════════════════════════════════════════════════
# Plan management  (Pricing v1)
# ═══════════════════════════════════════════════════════════════════════════════

@router.patch("/organizations/{org_id}/plan")
async def change_org_plan(
    org_id: int,
    body: ChangePlanRequest,
    request: Request,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Change an org's plan_code.

    - Switching to 'comp': optionally sets comp_until (default = today + 30 days).
    - Switching FROM 'comp' to a paying plan: clears comp_until.
    """
    from app.repositories.internal.audit_log_repo import db_log_audit_event  # noqa: PLC0415

    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")

    old_plan = org.get("plan_code") or org.get("subscription_plan") or "free"
    new_plan = body.plan_code

    # Resolve comp_until
    comp_until_val: Optional[str] = None
    if new_plan == "comp":
        if body.comp_until:
            comp_until_val = body.comp_until
        else:
            comp_until_val = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")

    updates: dict = {"plan_code": new_plan}
    if new_plan == "comp":
        updates["comp_until"] = comp_until_val
    else:
        # Switching away from comp — clear comp_until
        updates["comp_until"] = None

    # Execute the update via raw SQL (plan_code and comp_until are not in the
    # whitelisted _ALLOWED_ORG_FIELDS of db_update_organization — keep it direct here)
    from app.services.database import get_pool  # noqa: PLC0415
    pool = await get_pool()
    with bypass_tenant_scope("change_org_plan_superadmin"):
        async with pool.acquire() as conn:
            if new_plan == "comp":
                await conn.execute(
                    "UPDATE organizations SET plan_code=$1, comp_until=$2::timestamptz, updated_at=NOW() WHERE id=$3",
                    new_plan, comp_until_val, org_id,
                )
            else:
                await conn.execute(
                    "UPDATE organizations SET plan_code=$1, comp_until=NULL, updated_at=NOW() WHERE id=$2",
                    new_plan, org_id,
                )

    actor = request.headers.get("X-Superadmin-User", "superadmin")
    ip = request.client.host if request.client else None
    with bypass_tenant_scope("change_org_plan_audit"):
        await db_log_audit_event(
            actor=actor,
            action="org.plan_changed",
            target_type="organization",
            target_id=str(org_id),
            org_id=org_id,
            payload={"old_plan": old_plan, "new_plan": new_plan, "comp_until": comp_until_val},
            request_ip=ip,
        )

    log.info("change_org_plan", org_id=org_id, old=old_plan, new=new_plan, comp_until=comp_until_val)
    return _ok({"org_id": org_id, "old_plan": old_plan, "new_plan": new_plan, "comp_until": comp_until_val})


@router.patch("/organizations/{org_id}/comp")
async def set_org_comp(
    org_id: int,
    body: SetCompRequest,
    request: Request,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Set or clear comp_until for an org.

    Body: {"comp_until": "YYYY-MM-DD"} → sets plan_code='comp' + comp_until.
    Body: {"comp_until": null}         → clears comp_until + reverts plan_code to 'free'.
    """
    from app.repositories.internal.audit_log_repo import db_log_audit_event  # noqa: PLC0415

    org = await restaurant_repo.db_get_org_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organizacion no encontrada")

    old_plan = org.get("plan_code") or "free"
    from app.services.database import get_pool  # noqa: PLC0415
    pool = await get_pool()

    if body.comp_until is not None:
        new_plan = "comp"
        with bypass_tenant_scope("set_org_comp_superadmin"):
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE organizations SET plan_code='comp', comp_until=$1::timestamptz, updated_at=NOW() WHERE id=$2",
                    body.comp_until, org_id,
                )
    else:
        # Clearing comp — revert to free unless they had a paid plan before
        revert_plan = old_plan if old_plan != "comp" else "free"
        new_plan = revert_plan
        with bypass_tenant_scope("clear_org_comp_superadmin"):
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE organizations SET plan_code=$1, comp_until=NULL, updated_at=NOW() WHERE id=$2",
                    revert_plan, org_id,
                )

    actor = request.headers.get("X-Superadmin-User", "superadmin")
    ip = request.client.host if request.client else None
    with bypass_tenant_scope("set_org_comp_audit"):
        await db_log_audit_event(
            actor=actor,
            action="org.comp_changed",
            target_type="organization",
            target_id=str(org_id),
            org_id=org_id,
            payload={"old_plan": old_plan, "new_plan": new_plan, "comp_until": body.comp_until},
            request_ip=ip,
        )

    log.info("set_org_comp", org_id=org_id, comp_until=body.comp_until, new_plan=new_plan)
    return _ok({"org_id": org_id, "new_plan": new_plan, "comp_until": body.comp_until})


# ═══════════════════════════════════════════════════════════════════════════════
# User password reset
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/users/{username}/reset-password")
async def reset_user_password(
    username: str,
    body: ResetPasswordRequest,
    request: Request,
    _: None = Depends(verify_superadmin),
    _bypass: None = Depends(_bypass_internal_admin),
):
    """Reset a user's password and invalidate all their existing sessions.

    Uses username as the path parameter (it is the PK on the users table).
    """
    from app.repositories.internal.audit_log_repo import db_log_audit_event  # noqa: PLC0415

    # Verify user exists
    user = await restaurant_repo.db_get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    new_hash = hash_pw(body.new_password)
    updated = await restaurant_repo.db_update_user_password(username, new_hash)
    if not updated:
        raise HTTPException(status_code=500, detail="Error al actualizar contraseña")

    sessions_deleted = await sessions_repo.delete_sessions_for_user(username)

    actor = request.headers.get("X-Superadmin-User", "superadmin")
    ip = request.client.host if request.client else None
    with bypass_tenant_scope("reset_user_password_audit"):
        await db_log_audit_event(
            actor=actor,
            action="user.password_reset",
            target_type="user",
            target_id=username,
            payload={"sessions_deleted": sessions_deleted},
            request_ip=ip,
        )

    log.info("reset_user_password", username=username, sessions_deleted=sessions_deleted)
    return _ok({"username": username, "sessions_deleted": sessions_deleted})
