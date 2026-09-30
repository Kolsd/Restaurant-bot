"""
Staff roster endpoints: list/create/update/delete staff, list their sedes,
and the PIN login. (Shifts, tips, payroll, schedules, deductions, contracts
and attendance were deleted in the 2026-09 cleanup.)

All routes are protected by require_auth (via get_current_restaurant_scoped).

Layer rules:
  - HTTP parsing / validation only here.
  - Business logic lives in services/.
  - Raw SQL lives exclusively in database.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from passlib.context import CryptContext

from app.routes.deps import (
    get_current_restaurant_scoped, get_current_user, resolve_sede_filter,
)
from app.services import database as db
from app.services import plan_access, state_store
from app.repositories import sessions_repo
from app.services.logging import get_logger
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/staff", tags=["staff"])

# bcrypt context — 12 rounds is a good default for PIN hashing
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

_VALID_ROLES = {"mesero", "cocina", "bar", "caja", "gerente", "domiciliario", "otro"}


# ── Pydantic models ──────────────────────────────────────────────────────────

class StaffCreate(BaseModel):
    name:            str       = Field(..., min_length=1, max_length=100, description="Nombre(s)")
    last_name:       str       = Field("", max_length=100, description="Apellido(s)")
    role:            str       = Field("mesero", min_length=1, max_length=50)
    roles:           list[str] = Field(default_factory=list)
    password:        str       = Field(..., min_length=4, max_length=100)
    phone:           str       = Field("", max_length=30)
    document_number: str       = Field("", max_length=50)
    location_id:     int | None = Field(None, description="Sede del empleado")


class StaffUpdate(BaseModel):
    name:            str | None       = Field(None, min_length=1, max_length=100)
    role:            str | None       = Field(None, min_length=1, max_length=50)
    roles:           list[str] | None = None
    password:        str | None       = Field(None, min_length=4, max_length=100)
    phone:           str | None       = Field(None, max_length=30)
    active:          bool | None      = None
    document_number: str | None       = Field(None, max_length=50)
    location_id:     int | None       = Field(None, description="Sede del empleado")

class StaffPinLoginRequest(BaseModel):
    restaurant_id: int
    name: str = Field(..., min_length=1, max_length=100, description="Nombre completo o usuario (ej: juan.perez)")
    pin:  str = Field(..., min_length=4, max_length=100)


def _staff_redirect(roles: list) -> str:
    """Return the best landing page URL for the given role set.
    Admins/managers go to /dashboard.
    All operational staff go to /staff (the unified Staff App).
    """
    admin_roles = {"owner", "admin", "gerente"}
    if any(r in admin_roles for r in roles):
        return "/dashboard"
    return "/staff"


@router.get("")
async def list_staff(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
    user: dict = Depends(get_current_user),
):
    """Returns the staff the caller may see.

    owner/admin: the whole organization, or one sede when they pick it from
    the sidebar. Everyone else: their own sede (PM 2026-09-20) — a gerente
    or a cajero of one sede has no business reading another sede's roster,
    with its phone numbers and document numbers.

    The old note here said the header "doesn't apply" because passing it as
    a branch_id used to be read as an org_id and returned zero rows. The id
    kinds are separate now (`staff.location_id`), so it does apply.
    """
    org_id = restaurant["id"]
    sede = resolve_sede_filter(request, user)
    staff = await db.db_get_staff(org_id, location_id=sede if isinstance(sede, int) else None)

    # multi_sede tells the Team admin UI whether "Sin sede" is even a
    # meaningful thing to flag — a single-sede org auto-assigns every staff
    # member (docs/claude/delivery-web.md chunk 8: "single-sede orgs: no
    # selector needed"), so an unassigned row there would just be noise.
    from app.repositories import restaurant_repo  # noqa: PLC0415
    locations = await restaurant_repo.db_get_org_locations(org_id)
    return {"staff": staff, "multi_sede": len(locations) > 1}


async def _resolve_new_staff_location(org_id: int, requested: int | None) -> int | None:
    """Which sede a new staff member belongs to.

    - An explicit `requested` sede must belong to this org (403 otherwise —
      never trust a client-sent location id).
    - With none requested, an org with exactly ONE active sede gets that sede:
      there is nothing to choose, and leaving it NULL would lock the person out
      of every sede-scoped section (the cashier's Domicilios refuses a staff
      member without a sede).
    - With several sedes and none requested, stay NULL: picking one would be a
      guess. The team UI assigns it.
    """
    from app.repositories import restaurant_repo  # noqa: PLC0415

    locations = await restaurant_repo.db_get_org_locations(org_id)
    if requested is not None:
        if not any(int(loc["id"]) == int(requested) for loc in locations):
            raise HTTPException(status_code=403, detail="Esa sede no pertenece a tu organización")
        return int(requested)
    if len(locations) == 1:
        return int(locations[0]["id"])
    return None


async def _enforce_staff_cap(org_id: int) -> None:
    """Esencial allows 5 active staff users per sede (pricing 2026-09-30);
    the other plans, and every trial, have no cap."""
    from app.repositories import restaurant_repo  # noqa: PLC0415
    cap = await plan_access.org_staff_cap(org_id)
    if cap is None:
        return
    sedes = max(1, len(await restaurant_repo.db_get_org_locations(org_id)))
    with tenant_scope(org_id):
        roster = await db.db_get_staff(org_id)
    active = sum(1 for m in roster if m.get("active"))
    if active >= cap * sedes:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Tu plan incluye hasta {cap} usuarios por sede. "
                "Para agregar más, cambia al plan Restaurante."
            ),
        )


@router.post("", status_code=201)
async def create_staff(
    request: Request,
    body: StaffCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Creates a staff member in the authenticated user's organization.

    Wave-2: db_create_staff INSERTs INTO staff (org_id, ...) — the first
    argument is the TENANT KEY (org_id), not the branch. Pre-Step-10 the
    X-Branch-ID override turned branch_id into location_id and the INSERT
    wrote org_id = location_id (FK violation or assignment to another org).
    The X-Branch-ID header could be used in the future to populate
    staff.location_id (assign a staff member to a branch); for now
    db_create_staff doesn't take that param, so we ignore the header to
    avoid introducing an unsupported parameter.
    """
    org_id = restaurant["id"]

    pin_hash = _pwd_ctx.hash(body.password)
    roles = [r.strip().lower() for r in body.roles if r.strip()] if body.roles else [body.role.strip().lower()]
    full_name = f"{body.name.strip()} {body.last_name.strip()}".strip() if body.last_name else body.name.strip()

    location_id = await _resolve_new_staff_location(org_id, body.location_id)
    await _enforce_staff_cap(org_id)

    member = await db.db_create_staff(
        restaurant_id=org_id,
        name=full_name,
        role=roles[0] if roles else "mesero",
        pin_hash=pin_hash,
        phone=body.phone,
        roles=roles or ["mesero"],
        document_number=body.document_number,
        location_id=location_id,
    )
    return {"staff": member}


@router.get("/locations")
async def list_staff_locations(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Org's active sedes, for the sede picker on the Team admin UI (creating
    or editing a staff member — docs/claude/delivery-web.md chunk 8). A
    single-sede org has nothing to pick (the frontend auto-assigns, per the
    locked decision: "single-sede orgs: no selector needed"), but still gets
    a real answer here rather than a 404/empty special case.
    """
    from app.repositories import restaurant_repo  # noqa: PLC0415

    locations = await restaurant_repo.db_get_org_locations(restaurant["id"])
    return {"locations": [{"id": loc["id"], "name": loc["name"]} for loc in locations]}


_PIN_MAX_ATTEMPTS = 10
_PIN_WINDOW = 900  # 15 minutes
# Defense-in-depth: a global per-IP cap stops distributed brute force across
# many (restaurant_id, name) tuples. Without this an attacker iterating
# restaurant_id=1..1000 with name="Pedro"+pin="1234" gets 10 attempts per
# bucket — 10K total before any single bucket triggers. The global cap
# kicks in after 10 attempts per IP per 15min regardless of target.
# L-1: Reduced from 30 → 10; a legitimate user needs at most 2-3 tries.
_PIN_GLOBAL_MAX_ATTEMPTS = 10
_PIN_GLOBAL_WINDOW = 900  # 15 minutes


async def _check_pin_rate_limit(request: Request, restaurant_id: int, name: str) -> None:
    """Rate-limit PIN login via Redis (cross-worker safe).

    Two layers:
      1. Per (restaurant_id, name, IP) bucket — granular protection.
      2. Per IP global — stops cross-tenant brute force iteration.
    """
    ip = request.client.host if request.client else "unknown"

    # Layer 2: global per-IP cap (cross-restaurant).
    global_allowed = await state_store.rate_limit_check(
        key=f"pin_login_global:{ip}",
        max_requests=_PIN_GLOBAL_MAX_ATTEMPTS,
        window_seconds=_PIN_GLOBAL_WINDOW,
    )
    if not global_allowed:
        log.warning("staff.pin_login.global_rate_limit_hit", ip=ip)
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 15 minutos.")

    # Layer 1: granular bucket.
    key = f"pin_login:{restaurant_id}:{str(name).lower().strip()}:{ip}"
    allowed = await state_store.rate_limit_check(
        key=key, max_requests=_PIN_MAX_ATTEMPTS, window_seconds=_PIN_WINDOW
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 15 minutos.")


@router.post("/pin-login", status_code=200)
async def staff_pin_login(request: Request, body: StaffPinLoginRequest):
    await _check_pin_rate_limit(request, body.restaurant_id, body.name)

    # Pre-auth flow: scope the tenant explicitly from the body. The downstream
    # repos call `tenant_connection()` which requires an active scope; without
    # this wrapper every PIN login raises TenantNotSetError (regression caught
    # by tests/e2e/test_staff_pin_login_e2e.py).
    from app.services.tenant_context import tenant_scope

    with tenant_scope(body.restaurant_id):
        member = await db.db_get_staff_for_pin_login(body.restaurant_id, body.name)
    # Use a constant-time response regardless of whether the employee was found
    # or the PIN was wrong — prevents username enumeration oracle.
    if not member or not _pwd_ctx.verify(body.pin, member["pin"]):
        # Audit trail: every failed attempt logged for ops visibility.
        # Aggregating these across the fleet surfaces distributed brute-force
        # attempts that the per-bucket rate limit alone cannot stop.
        ip = request.client.host if request.client else "unknown"
        log.warning(
            "staff.pin_login_failed",
            ip=ip,
            restaurant_id=body.restaurant_id,
            name_len=len(str(body.name or "")),
            member_found=bool(member),
        )
        raise HTTPException(status_code=401, detail="Credenciales inválidas.")

    token = await sessions_repo.create_session(f"staff:{member['id']}")

    roles = member.get("roles") or [member.get("role", "mesero")]

    # body.restaurant_id is an org_id — db_get_staff_for_pin_login above
    # already queried `staff WHERE org_id=$1`, confirming the id kind.
    with tenant_scope(body.restaurant_id):
        restaurant_data = await db.db_get_restaurant_by_org_id(body.restaurant_id)
    raw_features = restaurant_data.get("features") or {} if restaurant_data else {}
    if isinstance(raw_features, str):
        import json as _j
        try: raw_features = _j.loads(raw_features)
        except (ValueError, TypeError): raw_features = {}

    return {
        "token":        token,
        "staff_id": member["id"],
        "roles":    roles,
        "name":     member["name"],
        "username": member.get("username", ""),
        "redirect": _staff_redirect(roles),
        "restaurant": {
            "name":             restaurant_data.get("name", "") if restaurant_data else "",
            "locale":           raw_features.get("locale", "es-CO"),
            "currency":         raw_features.get("currency", "COP"),
            "features":         raw_features,
        }
    }

@router.put("/{staff_id}")
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Update mutable staff fields. PIN is re-hashed if provided."""
    patch = body.model_dump(exclude_none=True)

    if "password" in patch:
        patch["pin"] = _pwd_ctx.hash(patch.pop("password"))

    if "roles" in patch:
        patch["roles"] = [r.strip().lower() for r in patch["roles"] if r.strip()]
        if patch["roles"] and "role" not in patch:
            patch["role"] = patch["roles"][0]

    if "location_id" in patch:
        # Same ownership validation as _resolve_new_staff_location — never
        # trust a client-sent location id without checking it belongs to
        # this org (rls-multitenant.md: org_id/location_id can collide
        # across tenants). `requested` is never None here (StaffUpdate's
        # location_id is excluded above when absent), so the "auto-pick the
        # org's only sede" branch of that helper never fires on this path.
        patch["location_id"] = await _resolve_new_staff_location(restaurant["id"], patch["location_id"])

    if not patch:
        raise HTTPException(status_code=422, detail="No fields to update.")

    updated = await db.db_update_staff(staff_id, restaurant["id"], patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    return {"staff": updated}


@router.delete("/{staff_id}", status_code=200)
async def delete_staff(
    staff_id: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Permanently deletes a staff member from the roster."""
    deleted = await db.db_delete_staff(staff_id, restaurant["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    return {"success": True}

