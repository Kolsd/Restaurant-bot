"""
Authentication dependencies.

WAVE 1 SEMANTICS (Bloque S3 of Org/Location migration):
  - get_current_restaurant / _scoped: LEGACY aliases.  Return Org shaped as
    restaurant dict (id = org.id, name = org.name, etc.) — unchanged behaviour.
  - get_current_org: preferred dep for routes that should scope on Org.
  - get_current_org_scoped: yield-based variant of get_current_org that enters
    tenant_scope(org_id).  Use for new RLS-compliant routes.
  - get_current_location / require_location: optional deps for routes that
    need a specific Location (validates X-Location-ID header ownership).

Route migration cadence:
  - New routes should use get_current_org_scoped + optional get_current_location.
  - Existing routes stay on get_current_restaurant_scoped until Bloque S6.
"""
from typing import Callable
from fastapi import Request, HTTPException, Depends
from app.services.auth import verify_token
from app.services import database as db
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.tenant_db import tenant_connection

_log = get_logger(__name__)


async def verify_superadmin(request: Request) -> None:
    """Validates that the Bearer token belongs to an active superadmin session.
    Raises 401 if missing, 403 if not a superadmin session.
    """
    from app.repositories import sessions_repo
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Autenticación requerida")
    identity = await sessions_repo.get_session(token)
    if identity != "superadmin":
        raise HTTPException(status_code=403, detail="Acceso exclusivo para el equipo Mesio")


async def require_auth(request: Request) -> str:
    """Validates Bearer token; returns username or raises 401."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return username

async def get_current_user(request: Request) -> dict:
    """Returns the authenticated user dict or raises 401."""
    username = await require_auth(request)

    if username.startswith("staff:"):
        staff_id = username.split(":", 1)[1]
        # Cross-tenant pre-auth: we don't know the org yet, so we bypass RLS
        # to look up the staff member by PK, then the rest of the request runs
        # under the correct tenant scope.
        with bypass_tenant_scope("deps.get_current_user.pre_auth_staff_resolution"):
            async with tenant_connection() as conn:
                # Note: s.org_id and s.restaurant_id both exist at DB revision 0036.
                # We use s.org_id aliased as restaurant_id because it is reliably populated
                # by the auto-populate trigger for every row, and is the canonical tenant key
                # going forward (Wave 1).  For Matriz restaurants org_id == restaurant_id so
                # downstream code that treats this value as "restaurant_id" still works.
                # The JOIN to restaurants is dropped: parent_restaurant_id is unused by
                # callers (staff is always scoped to an Org/Location, not a legacy branch).
                query = """
                    SELECT s.org_id AS restaurant_id, s.role, s.roles,
                           NULL::int AS parent_restaurant_id
                    FROM staff s
                    WHERE s.id::text = $1
                """
                staff_member = await conn.fetchrow(query, str(staff_id))

            if staff_member:
                # branch_id is always the restaurant_id (parent or branch)
                mapped_branch_id = staff_member["restaurant_id"]

                raw_roles = staff_member["roles"]
                if isinstance(raw_roles, list):
                    roles_list = raw_roles
                elif isinstance(raw_roles, str):
                    import json as _j
                    try:
                        roles_list = _j.loads(raw_roles)
                    except Exception:
                        roles_list = []
                else:
                    roles_list = []

                if not roles_list and staff_member["role"]:
                    roles_list = [staff_member["role"]]

                combined_role = ",".join(roles_list) if roles_list else (staff_member["role"] or "")

                return {
                    "username": username,
                    "branch_id": mapped_branch_id,
                    "restaurant_id": staff_member["restaurant_id"],
                    # Explicit tenant key (P0 fix 2026-09) — staff.org_id is
                    # always the org id, never ambiguous like users.branch_id.
                    "org_id": staff_member["restaurant_id"],
                    "location_id": None,
                    "role": combined_role
                }

    user = await db.db_get_user(username)
    if user:
        return user

    raise HTTPException(status_code=401, detail="User not found")

async def get_current_restaurant(request: Request) -> dict:
    """Returns the restaurant for the authenticated user or raises 403.

    P0 fix (2026-09): previously resolved via `user["branch_id"]`, a column
    with NO fixed id-kind contract — some writers stored an org_id there,
    others a location_id, and the now-deleted `db_get_restaurant_by_id`
    guessed between the two, silently preferring the ORG match whenever a
    location id collided with an unrelated org's id. That could serve — or
    tenant_scope() a user into — a completely different tenant.

    This now resolves ONLY through the explicit `org_id` / `location_id`
    fields on the user dict (backfilled onto `users.org_id` /
    `users.location_id` by the users_org_location migration, and populated
    directly on the staff dict in get_current_user above). A user whose
    org_id could not be resolved (genuine legacy ambiguity) is DENIED —
    never guessed via name-match or branch_id fallback.
    """
    user = await get_current_user(request)

    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=403, detail="Restaurant not found")
    org_id = int(org_id)

    location_id = user.get("location_id")
    default_rest = None
    if location_id:
        default_rest = await db.db_get_restaurant_by_location_id(int(location_id))
        # Ownership sanity check — a user's own location_id should always
        # belong to their own org_id, but never trust that without checking.
        if not default_rest or default_rest.get("org_id") != org_id:
            default_rest = None

    if default_rest is None:
        default_rest = await db.db_get_restaurant_by_org_id(org_id)
    if default_rest is None:
        raise HTTPException(status_code=403, detail="Restaurant not found")

    # 🛡️ MAGIA MULTI-SUCURSAL: Si el owner envía la cabecera, suplanta la sede.
    # X-Branch-ID SIEMPRE carga un location_id. The selected sede must belong
    # to the SAME org as the authenticated user — verified via org_id.
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        target_id = int(branch_header)
        target_rest = await db.db_get_restaurant_by_location_id(target_id)
        if (
            target_rest
            and target_rest.get("org_id")
            and target_rest.get("org_id") == org_id
        ):
            return target_rest

    return default_rest


# NOTE: Decision — get_current_restaurant is called as a regular async function
# from many route files (inventory, stats, tables, nps, etc.), not only via
# Depends().  Mutating it to a yield-based generator would break all those
# call sites.  Instead we provide this sibling that wraps the resolved
# restaurant in tenant_scope() and is used ONLY by loyalty routes (the RLS
# pilot).  Other routes continue to use the original get_current_restaurant.
async def get_current_restaurant_scoped(request: Request):
    """Yield-based variant of get_current_restaurant that activates tenant_scope.

    Used by loyalty routes as the RLS pilot.  Entering tenant_scope() pins
    app.restaurant_id for every DB call made within the request lifetime.
    The scope is guaranteed to exit via the finally clause in the `with` block.

    DO NOT use this dep in routes that also call get_current_restaurant() as a
    plain function — the scope would be active only for the Depends() path.
    """
    from app.services.tenant_context import tenant_scope

    restaurant = await get_current_restaurant(request)
    with tenant_scope(restaurant["id"]):
        yield restaurant


async def get_current_user_scoped(request: Request):
    """Yield-based variant of get_current_user that activates tenant_scope.

    For routes that only need the user dict (staff login, profile, etc.) and
    whose downstream repo calls are tenant-scoped via the user's restaurant_id.
    """
    from app.services.tenant_context import tenant_scope

    user = await get_current_user(request)
    # P0 fix (2026-09): prefer the explicit org_id (unambiguous) over the
    # legacy restaurant_id/branch_id fields, which for admin/owner users
    # come straight from the mixed-kind users.branch_id column.
    rid = user.get("org_id") or user.get("restaurant_id") or user.get("branch_id")
    if rid:
        with tenant_scope(int(rid)):
            yield user
    else:
        # User without a restaurant (unusual — probably superadmin bootstrap).
        # Yield without scope; downstream tenant-scoped repo calls will raise
        # TenantNotSetError if reached, which is the correct fail-loud behaviour.
        yield user


def require_module(module_name: str) -> Callable:
    """
    FastAPI dependency factory for module-level access control.

    Reads features directly from the already-loaded restaurant dict to avoid
    a second DB round-trip and normalisation mismatches in db_check_module.
    Accepts both boolean True and the string "true" as enabled values.

    Raises:
        401 — if the Bearer token is missing or invalid (via get_current_restaurant).
        403 — if the restaurant exists but does not have the module enabled.
    """
    import json as _json

    async def _check_module(
        restaurant: dict = Depends(get_current_restaurant),
    ) -> None:
        features = restaurant.get("features") or {}
        if isinstance(features, str):
            try:
                features = _json.loads(features)
            except Exception:
                features = {}
        if not isinstance(features, dict):
            features = {}
        val = features.get(module_name)
        has_module = val is True or str(val).lower() == "true"
        if not has_module:
            raise HTTPException(
                status_code=403,
                detail=f"El restaurante no tiene activo el módulo: {module_name}",
            )

    return _check_module

# At the end of the file, after the existing functions

ROLE_PAGE_MAP = {
    "/dashboard":   {"owner", "admin", "gerente"},
    "/settings":    {"owner", "admin", "gerente"},
    "/billing":     {"owner", "admin", "gerente"},
    # /staff (the unified Staff App) has no single allowed-roles set here —
    # every staff/admin role may load it; which sections it shows per role
    # is decided by app.services.staff_sections, not this map.
}

ADMIN_ROLES = {"owner", "admin", "gerente"}

def _extract_roles(role_str: str) -> set:
    return {r.strip().lower() for r in (role_str or "").split(",") if r.strip()}

async def require_page_access(request: Request, path: str):
    """
    Verifies token + role to serve a protected HTML page.
    Redirects to /login if there's no token, to /staff if the role doesn't match.
    """
    from app.services.auth import verify_token
    from app.services import database as db

    token = None
    # Look up token in cookie or header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
    # HTML pages don't send an Authorization header — the token lives in localStorage
    # so for page routes, we return the HTML and let the JS validate
    # BUT: we can read a cookie if one exists
    token = request.cookies.get("rb_token") or token

    allowed_roles = ROLE_PAGE_MAP.get(path, set())
    if not allowed_roles:
        return None  # route has no restriction defined, let it through

    if not token:
        return None  # no cookie, the JS in the HTML will handle the redirect

    username = await verify_token(token)
    if not username:
        return None

    # Get the user's role
    if username.startswith("staff:"):
        staff_id = username.replace("staff:", "")
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role, roles FROM staff WHERE id=$1::uuid", staff_id
            )
        if not row:
            return None
        roles_list = row.get("roles") or []
        if not roles_list and row.get("role"):
            roles_list = [row["role"]]
        user_roles = {r.lower() for r in roles_list}
    else:
        user = await db.db_get_user(username)
        if not user:
            return None
        user_roles = _extract_roles(user.get("role", ""))

    # Admin can always access everything
    if user_roles & ADMIN_ROLES:
        return None  # allow

    # Check whether they have any role allowed for this page
    if not (user_roles & allowed_roles):
        raise HTTPException(status_code=403, detail="Rol no autorizado para esta página")

    return None


# ── Org/Location dependencies (Bloque S3) ────────────────────────────────────
#
# Wave 1 semantics:
#   - get_current_restaurant / _scoped: UNCHANGED — legacy aliases that return
#     the same shape as before.  All existing routes continue to work.
#   - get_current_org: new preferred dep.  During Wave 1 the org_id equals the
#     old restaurant_id (guaranteed by migration 0034), so passing it to
#     tenant_scope() works correctly.
#   - get_current_org_scoped: yield-based, enters tenant_scope(org_id).
#   - get_current_location / require_location: validate X-Location-ID header.


async def _resolve_org_id_for_user(user: dict) -> int | None:
    """Resolve the org_id for a user dict.

    P0 fix (2026-09): reads ONLY the explicit `org_id` field — set directly
    on the staff dict in get_current_user (from staff.org_id, canonical),
    and backfilled onto `users.org_id` by the users_org_location migration
    for admin/owner users. The old fallback guessed the org_id from the
    mixed-kind `users.branch_id` column (via db_get_location_by_id, treating
    branch_id as if it were always a location id) — for orgs where branch_id
    actually held an org_id, or where it collided with an unrelated org's
    location id, that guess could resolve to the WRONG tenant. A user whose
    org_id genuinely could not be backfilled (logged by the migration) is
    correctly denied here — never guessed.
    """
    org_id = user.get("org_id")
    return int(org_id) if org_id else None


async def get_current_org(request: Request) -> dict:
    """Return the Organization dict for the authenticated user.

    Raises 403 if no Org can be resolved.

    Wave 1: the Org id equals the old restaurant_id for Matrizes.  For staff
    whose restaurant_id is a Sucursal, the mapping table translates to the
    correct parent Org id.

    The result is cached on request.state.mesio_org to avoid duplicate DB
    lookups when both get_current_org and get_current_location are used as
    dependencies in the same request.
    """
    cached = getattr(request.state, "mesio_org", None)
    if cached is not None:
        return cached

    user = await get_current_user(request)
    org_id = await _resolve_org_id_for_user(user)
    if not org_id:
        raise HTTPException(status_code=403, detail="Organization not found")

    org = await db.db_get_org_by_id(org_id)
    if not org:
        # Fallback: shape a minimal org from restaurant data so legacy code keeps
        # working even before the organizations table is populated.
        r = await get_current_restaurant(request)
        import json as _json  # noqa: PLC0415
        feats = r.get("features") or {}
        if isinstance(feats, str):
            try:
                feats = _json.loads(feats)
            except Exception:
                feats = {}
        org = {
            "id":              r.get("id"),
            "name":            r.get("name"),
            "whatsapp_number": r.get("whatsapp_number"),
            "features":        feats,
            "subscription_plan": r.get("subscription_plan", "free"),
            "subscription_status": r.get("subscription_status", "active"),
        }

    request.state.mesio_org = org
    return org


async def get_current_org_scoped(request: Request):
    """Yield-based dep that enters tenant_scope(org_id) for the request.

    Preferred replacement for get_current_restaurant_scoped on new routes.

    Wave 1 reasoning (see ORG_LOCATION_MIGRATION_PLAN.md §4.5):
      tenant_scope(org_id) sets BOTH GUCs:
        app.restaurant_id = org_id  → legacy tenant_isolation policy matches
                                       Matriz rows (restaurant_id == org_id)
        app.org_id = org_id         → new org_isolation policy matches ALL rows
                                       belonging to this Org (org_id == org_id)
      So calling tenant_scope with the org_id gives full RLS coverage under
      both policies for the duration of the request.
    """
    from app.services.tenant_context import tenant_scope  # noqa: PLC0415

    org = await get_current_org(request)
    org_id = org.get("id")
    if not org_id:
        raise HTTPException(status_code=403, detail="Organization ID not found")

    with tenant_scope(int(org_id)):
        yield org


async def get_current_location(
    request: Request,
    org: dict = Depends(get_current_org),
) -> "dict | None":
    """Resolve Location from the X-Location-ID request header.

    Returns:
      dict  — the Location row if X-Location-ID is a valid int and belongs to
              the current Org.
      None  — if the header is missing or equal to "all" (queries without
              location filter are allowed).

    Raises 403 if the location_id belongs to a different Org (cross-org spoofing
    attempt).
    Raises 400 if the header value is non-numeric and not "all".
    """
    loc_header = request.headers.get("X-Location-ID", "").strip()
    if not loc_header or loc_header.lower() == "all":
        return None

    if not loc_header.isdigit():
        raise HTTPException(
            status_code=400,
            detail=f"X-Location-ID must be a positive integer or 'all', got {loc_header!r}",
        )

    location_id = int(loc_header)
    loc = await db.db_get_location_by_id(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    # Cross-org ownership check
    if loc.get("org_id") != org.get("id"):
        raise HTTPException(
            status_code=403,
            detail="Location does not belong to your organization",
        )

    return loc


async def require_location(
    request: Request,
    location: "dict | None" = Depends(get_current_location),
) -> dict:
    """Like get_current_location but raises 400 if no Location is specified.

    Use for routes where a specific Location is mandatory (e.g. staff clock-in,
    per-sede inventory edits).
    """
    if location is None:
        raise HTTPException(
            status_code=400,
            detail="This endpoint requires a specific X-Location-ID header",
        )
    return location