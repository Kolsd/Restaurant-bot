"""
Team routes: branch CRUD and user/team management for restaurant owners and admins.
"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.services.auth import hash_password
from app.services import database as db
from app.repositories import restaurant_repo
from app.routes.deps import get_current_user, may_span_locations, resolve_sede_filter
from app.services.tenant_context import tenant_scope
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

_STAFF_ROLES = {"mesero", "cocina", "caja", "gerente", "domiciliario", "bar", "otro"}


# ── Pydantic models ──────────────────────────────────────────────────

class TeamInviteRequest(BaseModel):
    username: str
    password: str = ""
    pin: str = ""
    role: str = "mesero"
    phone: str = ""
    branch_id: int = None


class CreateBranchRequest(BaseModel):
    name: str
    address: str
    latitude: float = None
    longitude: float = None


# ── BRANCHES ─────────────────────────────────────────────────────────

@router.get("/api/team/branches")
async def list_team_branches(request: Request):
    user = await get_current_user(request)
    roles_list = [r.strip() for r in (user.get("role") or "").split(",")]
    if "owner" not in roles_list:
        raise HTTPException(status_code=403, detail="Acceso restringido a dueños")

    # P0 fix (2026-09): db_get_branches expects an org_id (its docstring is
    # explicit about this) — the old user["branch_id"] is mixed-kind and,
    # for any user whose branch_id actually held a location_id, silently
    # returned zero rows instead of the org's branches.
    org_id = user.get("org_id")
    if not org_id:
        return {"branches": []}

    with tenant_scope(int(org_id)):
        branches = await restaurant_repo.db_get_branches(int(org_id))
    return {"branches": branches}


@router.post("/api/team/branches")
async def create_branch(request: Request, body: CreateBranchRequest):
    """Add a sede to the owner's organization.

    A sede is a `locations` row of the org: the carta and features are the
    org's (plus that sede's own overrides, 0093), so nothing is copied.
    Fixed 2026-09-25: this created a whole new ORGANIZATION through the
    legacy `db_create_restaurant` (a copy of the menu and features) and then
    tried to re-parent its sede by a WhatsApp number that sede never had, so
    the owner never got the sede — and since 0034 it 500'd outright on an
    ON CONFLICT with no matching unique index.
    """
    from app.routes.dashboard import geocode_address
    user = await get_current_user(request)
    roles_list = [r.strip() for r in (user.get("role") or "").split(",")]
    if "owner" not in roles_list:
        raise HTTPException(status_code=403, detail="Solo el dueño puede crear sucursales")

    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Tu usuario no tiene una organización asignada.")
    org_id = int(org_id)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="La sede necesita un nombre.")

    lat, lon, display = body.latitude, body.longitude, ""
    if lat is None or lon is None:
        lat, lon, display = await geocode_address(body.address)

    with tenant_scope(org_id):
        location = await restaurant_repo.db_create_location(
            org_id, name, address=body.address, latitude=lat, longitude=lon,
        )
    log.info("team.branch_created", org_id=org_id, location_id=location["id"])
    return {
        "success": True, "location_id": location["id"],
        "latitude": lat, "longitude": lon, "display_name": display,
    }


@router.delete("/api/team/branches/{branch_id}")
async def delete_branch(branch_id: int, request: Request):
    user = await get_current_user(request)
    roles_list = [r.strip() for r in (user.get("role") or "").split(",")]
    if "owner" not in roles_list:
        raise HTTPException(status_code=403, detail="Solo el dueño puede eliminar sucursales")

    # P0 fix (2026-09): resolve ownership via the explicit org_id directly —
    # no DB round-trip to guess it, and no risk of the old branch_id guess
    # resolving to an unrelated org. `branch_id` (path param) is a
    # LOCATION id, verified below via db_get_restaurant_by_location_id.
    my_org_id = user.get("org_id")
    if not my_org_id:
        raise HTTPException(status_code=403, detail="Tu usuario no tiene una organización asignada")
    my_org_id = int(my_org_id)

    # Wave-2 model has no "Matriz" — this guard only protects the caller's
    # own explicitly-assigned location, when they have one.
    my_location_id = user.get("location_id")
    if my_location_id and branch_id == int(my_location_id):
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propia sucursal asignada desde aquí.")

    branch_row = await db.db_get_restaurant_by_location_id(branch_id)
    if not branch_row or branch_row.get("org_id") != my_org_id:
        log.warning(
            "team.delete_branch_idor_attempt",
            requested_branch_id=branch_id,
            user_org_id=my_org_id,
        )
        raise HTTPException(status_code=404, detail="La sucursal no existe o no pertenece a tu cuenta.")

    # db_delete_branch re-verifies ownership itself via a real sibling
    # location id from the same org (never the target branch_id itself —
    # that would make its internal check trivially true).
    parent_location_id = my_location_id
    if not parent_location_id:
        default_loc = await restaurant_repo.db_get_default_location(my_org_id)
        parent_location_id = default_loc["id"] if default_loc else None
    if not parent_location_id:
        raise HTTPException(status_code=404, detail="La sucursal no existe o no pertenece a tu cuenta.")

    deleted = await restaurant_repo.db_delete_branch(branch_id, parent_location_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="La sucursal no existe o no pertenece a tu cuenta.")
    return {"success": True}


# ── USERS / TEAM ─────────────────────────────────────────────────────

@router.get("/api/team/users")
async def list_team_users(request: Request, branch_id: int = None):
    user = await get_current_user(request)

    # P0 fix (2026-09): resolve the caller's org via the explicit org_id
    # field directly (no DB round-trip, no branch_id guess). `branch_id`
    # (query param or X-Branch-ID header) is always a LOCATION id and is
    # verified below to belong to the caller's own org before use.
    my_org_id = user.get("org_id")
    if not my_org_id:
        raise HTTPException(status_code=403, detail="Tu usuario no tiene una organización asignada")
    my_org_id = int(my_org_id)

    # Sede scoping (PM 2026-09-20). Only owner/admin may list another sede's
    # team or the org-wide roster; anyone else sees their own sede. Until now
    # ANY authenticated account — a waiter, a cook — could list every employee
    # of every sede just by omitting the filter.
    if may_span_locations(user):
        branch_header = request.headers.get("X-Branch-ID")
        if not branch_id and branch_header and branch_header.isdigit():
            branch_id = int(branch_header)
    else:
        branch_id = resolve_sede_filter(request, user)

    if branch_id:
        branch_row = await db.db_get_restaurant_by_location_id(branch_id)
        if not branch_row or branch_row.get("org_id") != my_org_id:
            log.warning(
                "team.users_idor_attempt",
                requested_branch_id=branch_id,
                user_org_id=my_org_id,
            )
            raise HTTPException(status_code=403, detail="No autorizado para ver usuarios de esta sucursal")

    users = await restaurant_repo.db_get_team_users(my_org_id, location_id=branch_id)
    return {"users": users}


@router.post("/api/team/invite")
async def team_invite(request: Request, body: TeamInviteRequest):
    from passlib.context import CryptContext as _CC
    _pin_ctx = _CC(schemes=["bcrypt"], deprecated="auto")

    creator = await get_current_user(request)
    roles_list = [r.strip() for r in (creator.get("role") or "").split(",")]
    is_owner = "owner" in roles_list
    is_admin = "admin" in roles_list
    if not is_owner and not is_admin:
        raise HTTPException(status_code=403, detail="No autorizado")

    # P0 fix (2026-09): resolve the creator's own org via the explicit
    # org_id field directly — no DB round-trip, no branch_id guess.
    my_org_id = creator.get("org_id")
    if not my_org_id:
        raise HTTPException(status_code=403, detail="Tu usuario no tiene una organización asignada")
    my_org_id = int(my_org_id)

    # branch_id here is a LOCATION id: body.branch_id comes from an owner's
    # branch-picker (populated from db_get_branches, which returns location
    # rows), and the admin fallback is their own explicitly-assigned sede.
    branch_id = body.branch_id if is_owner else creator.get("location_id")
    if not branch_id:
        branch_id = creator.get("location_id")
    if not branch_id:
        # No specific sede resolvable — fall back to the org's own
        # deterministic default location (never a "primary" judgement call).
        default_loc = await restaurant_repo.db_get_default_location(my_org_id)
        branch_id = default_loc["id"] if default_loc else None

    if not branch_id:
        raise HTTPException(status_code=400, detail="Sucursal requerida")
    branch_id = int(branch_id)

    # Verify the branch belongs to this owner's tenant (prevents cross-tenant invite)
    branch_check = await db.db_get_restaurant_by_location_id(branch_id)
    if not branch_check or branch_check.get("org_id") != my_org_id:
        log.warning(
            "team.invite_idor_attempt",
            requested_branch_id=branch_id,
            user_org_id=my_org_id,
        )
        raise HTTPException(status_code=403, detail="No autorizado para esta sucursal")

    branch = branch_check

    if body.role in ("admin", "gerente"):
        if not body.password:
            raise HTTPException(status_code=400, detail="Contraseña requerida para administrador o gerente")
        success = await db.db_create_user(
            body.username, hash_password(body.password), branch["name"],
            role=body.role, branch_id=branch_id, parent_user=creator["username"],
            org_id=my_org_id, location_id=branch_id,
        )
        if not success:
            raise HTTPException(status_code=400, detail="Usuario ya existe")
    else:
        if not body.pin:
            raise HTTPException(status_code=400, detail="PIN requerido para este rol")
        if len(body.pin) < 4:
            raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")
        roles = [r.strip() for r in body.role.split(",") if r.strip() in _STAFF_ROLES]
        if not roles:
            roles = ["mesero"]
        pin_hash = _pin_ctx.hash(body.pin)
        # P0 fix (2026-09): db_create_staff's first positional arg becomes
        # staff.org_id (INSERT INTO staff (org_id, ...)) — it MUST be the
        # org_id, not the location_id. Passing branch_id (a location id)
        # here used to corrupt staff.org_id for every non-admin team member
        # invited this way, silently working only when the Matriz invariant
        # (org_id == location_id) happened to hold.
        await db.db_create_staff(
            restaurant_id=my_org_id,
            name=body.username,
            role=roles[0],
            pin_hash=pin_hash,
            phone=body.phone,
            roles=roles,
            # branch_id was resolved and checked against my_org_id above.
            # Without it the staff member had no sede, and every
            # sede-scoped section (the cashier's Domicilios) refused them.
            location_id=branch_id,
        )

    return {"success": True}


@router.delete("/api/team/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    creator = await get_current_user(request)
    role = creator.get("role", "owner")
    if "owner" not in role and "admin" not in role:
        raise HTTPException(status_code=403, detail="No autorizado")

    # P0 fix (2026-09): compare the explicit org_id directly — no DB
    # round-trip, no branch_id guess.
    my_org_id = creator.get("org_id")

    target = await db.db_get_user(user_id)
    if target:
        if "admin" in role and "owner" not in role and target.get("org_id") != my_org_id:
            raise HTTPException(status_code=403, detail="No autorizado")
        await restaurant_repo.db_delete_user_by_username(user_id)
        return {"success": True}

    # Wave-2: verify staff cross-tenant ownership before deleting.
    # Return 404 (not 403) to avoid info-leaking whether the ID exists.
    from app.repositories import staff_repo as _sr
    staff_row = await _sr.db_get_staff_profile(user_id)
    if staff_row:
        if not my_org_id or staff_row.get("org_id") != my_org_id:
            log.warning(
                "team.delete_user_idor_attempt",
                user_id=user_id,
                staff_org_id=staff_row.get("org_id"),
                creator_org_id=my_org_id,
            )
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        with tenant_scope(my_org_id):
            deleted = await restaurant_repo.db_delete_staff_by_id(user_id)
    else:
        deleted = await restaurant_repo.db_delete_staff_by_id(user_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {"success": True}
