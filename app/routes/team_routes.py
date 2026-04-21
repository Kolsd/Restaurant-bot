"""
Team routes: branch CRUD and user/team management for restaurant owners and admins.
"""
import time
import json
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.services.auth import hash_password
from app.services import database as db
from app.repositories import restaurant_repo
from app.routes.deps import get_current_user
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
    whatsapp_number: str = ""
    address: str
    menu: dict = {}
    latitude: float = None
    longitude: float = None


# ── BRANCHES ─────────────────────────────────────────────────────────

@router.get("/api/team/branches")
async def list_team_branches(request: Request):
    user = await get_current_user(request)
    roles_list = [r.strip() for r in (user.get("role") or "").split(",")]
    if "owner" not in roles_list:
        raise HTTPException(status_code=403, detail="Acceso restringido a dueños")

    my_restaurant_id = user.get("branch_id")
    if not my_restaurant_id:
        return {"branches": []}

    with tenant_scope(my_restaurant_id):
        branches = await restaurant_repo.db_get_branches(my_restaurant_id)
    return {"branches": branches}


@router.post("/api/team/branches")
async def create_branch(request: Request, body: CreateBranchRequest):
    from app.routes.dashboard import geocode_address
    user = await get_current_user(request)
    roles_list = [r.strip() for r in (user.get("role") or "").split(",")]
    if "owner" not in roles_list:
        raise HTTPException(status_code=403, detail="Solo el dueño puede crear sucursales")

    my_restaurant_id = user.get("branch_id")
    if not my_restaurant_id:
        raise HTTPException(status_code=400, detail="Tu usuario no tiene un branch_id (Matriz) asignado.")

    wa_number = body.whatsapp_number.strip()

    with tenant_scope(my_restaurant_id):
        matriz_row = await restaurant_repo.db_get_matriz_details(my_restaurant_id)
    if not matriz_row:
        raise HTTPException(status_code=404, detail="No se encontró la Casa Matriz.")

    if not wa_number:
        wa_number = f"{matriz_row['whatsapp_number']}_b{int(time.time())}"

    menu_heredado = matriz_row['menu']
    if isinstance(menu_heredado, str):
        try:
            menu_heredado = json.loads(menu_heredado)
            if isinstance(menu_heredado, str):
                menu_heredado = json.loads(menu_heredado)
        except Exception:
            menu_heredado = {}
    elif not menu_heredado:
        menu_heredado = {}

    features_heredado = matriz_row['features']
    if isinstance(features_heredado, str):
        try:
            features_heredado = json.loads(features_heredado)
            if isinstance(features_heredado, str):
                features_heredado = json.loads(features_heredado)
        except Exception:
            features_heredado = {}
    elif not features_heredado:
        features_heredado = {}

    lat = body.latitude
    lon = body.longitude
    display = ""

    if lat is None or lon is None:
        lat, lon, display = await geocode_address(body.address)

    await db.db_create_restaurant(
        name=body.name,
        whatsapp_number=wa_number,
        address=body.address,
        menu=menu_heredado,
        latitude=lat,
        longitude=lon,
        features=features_heredado
    )

    await restaurant_repo.db_set_branch_parent(
        wa_number,
        my_restaurant_id,
        matriz_row.get('wa_phone_id', ''),
        matriz_row.get('wa_access_token', ''),
    )

    return {"success": True, "latitude": lat, "longitude": lon, "display_name": display}


@router.delete("/api/team/branches/{branch_id}")
async def delete_branch(branch_id: int, request: Request):
    user = await get_current_user(request)
    roles_list = [r.strip() for r in (user.get("role") or "").split(",")]
    if "owner" not in roles_list:
        raise HTTPException(status_code=403, detail="Solo el dueño puede eliminar sucursales")

    my_main_id = user.get("branch_id")
    if not my_main_id:
        raise HTTPException(status_code=403, detail="Tu usuario no tiene una Casa Matriz asignada")

    if branch_id == my_main_id:
        raise HTTPException(status_code=400, detail="No puedes eliminar la Casa Matriz desde aquí.")

    # Wave-2 ownership: resolve the org_id of the authenticated owner, then
    # compare against the target branch's org_id (both via VIEW normalization).
    my_rest = await db.db_get_restaurant_by_id(my_main_id)
    my_org_id = (my_rest or {}).get("org_id") or my_main_id
    branch_row = await db.db_get_restaurant_by_id(branch_id)
    if not branch_row or branch_row.get("org_id") != my_org_id:
        log.warning(
            "team.delete_branch_idor_attempt",
            requested_branch_id=branch_id,
            user_org_id=my_org_id,
        )
        raise HTTPException(status_code=404, detail="La sucursal no existe o no pertenece a tu cuenta.")

    deleted = await restaurant_repo.db_delete_branch(branch_id, my_main_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="La sucursal no existe o no pertenece a tu cuenta.")
    return {"success": True}


# ── USERS / TEAM ─────────────────────────────────────────────────────

@router.get("/api/team/users")
async def list_team_users(request: Request, branch_id: int = None):
    user = await get_current_user(request)

    branch_header = request.headers.get("X-Branch-ID")
    if not branch_id and branch_header and branch_header.isdigit():
        branch_id = int(branch_header)
    elif not branch_id:
        branch_id = user.get("branch_id")

    my_main_location_id = user.get("branch_id") or user.get("restaurant_id")

    # Wave-2 ownership: resolve the org_id of the authenticated owner, then
    # compare against the target branch's org_id (both via VIEW normalization).
    if branch_id and branch_id != my_main_location_id:
        my_rest = await db.db_get_restaurant_by_id(my_main_location_id)
        my_org_id = (my_rest or {}).get("org_id") or my_main_location_id
        branch_row = await db.db_get_restaurant_by_id(branch_id)
        if not branch_row or branch_row.get("org_id") != my_org_id:
            log.warning(
                "team.users_idor_attempt",
                requested_branch_id=branch_id,
                user_org_id=my_org_id,
            )
            raise HTTPException(status_code=403, detail="No autorizado para ver usuarios de esta sucursal")

    users = await restaurant_repo.db_get_team_users(branch_id)
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

    branch_id = body.branch_id if is_owner else creator.get("branch_id")

    if not branch_id and is_owner:
        branch_id = creator.get("branch_id")

    if not branch_id:
        raise HTTPException(status_code=400, detail="Sucursal requerida")

    # Verify the branch belongs to this owner's tenant (prevents cross-tenant invite)
    # Wave-2: compare org_id — parent_restaurant_id column was dropped in 0038.
    my_location_id = creator.get("branch_id") or creator.get("restaurant_id")
    if branch_id != my_location_id:
        my_rest = await db.db_get_restaurant_by_id(my_location_id)
        my_org_id = (my_rest or {}).get("org_id") or my_location_id
        branch_check = await db.db_get_restaurant_by_id(branch_id)
        if not branch_check or branch_check.get("org_id") != my_org_id:
            log.warning(
                "team.invite_idor_attempt",
                requested_branch_id=branch_id,
                user_location_id=my_location_id,
                user_org_id=my_org_id,
            )
            raise HTTPException(status_code=403, detail="No autorizado para esta sucursal")

    branch = await db.db_get_restaurant_by_id(branch_id)

    if body.role in ("admin", "gerente"):
        if not body.password:
            raise HTTPException(status_code=400, detail="Contraseña requerida para administrador o gerente")
        success = await db.db_create_user(
            body.username, hash_password(body.password), branch["name"],
            role=body.role, branch_id=branch_id, parent_user=creator["username"],
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
        await db.db_create_staff(
            restaurant_id=branch_id,
            name=body.username,
            role=roles[0],
            pin_hash=pin_hash,
            phone=body.phone,
            roles=roles,
        )

    return {"success": True}


@router.delete("/api/team/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    creator = await get_current_user(request)
    role = creator.get("role", "owner")
    if "owner" not in role and "admin" not in role:
        raise HTTPException(status_code=403, detail="No autorizado")

    target = await db.db_get_user(user_id)
    if target:
        if "admin" in role and "owner" not in role and target.get("branch_id") != creator.get("branch_id"):
            raise HTTPException(status_code=403, detail="No autorizado")
        await restaurant_repo.db_delete_user_by_username(user_id)
        return {"success": True}

    # Wave-2: verify staff cross-tenant ownership before deleting.
    # Return 404 (not 403) to avoid info-leaking whether the ID exists.
    from app.repositories import staff_repo as _sr
    staff_row = await _sr.db_get_staff_profile(user_id)
    if staff_row:
        creator_location_id = creator.get("branch_id") or creator.get("restaurant_id")
        my_rest = await db.db_get_restaurant_by_id(creator_location_id)
        my_org_id = (my_rest or {}).get("org_id") or creator_location_id
        if staff_row.get("org_id") != my_org_id:
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
