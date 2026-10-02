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
    if identity != sessions_repo.SUPERADMIN_IDENTITY:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para el equipo Mesio")


async def require_auth(request: Request) -> str:
    """Validates Bearer token; returns username or raises 401."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return username

async def get_current_user(request: Request) -> dict:
    """Returns the authenticated user dict or raises 401.

    Resolved once per request and kept in the request's ASGI `scope`. A route
    behind `get_current_restaurant_scoped` runs pinned to its tenant, and
    resolving a staff login a second time from inside it (e.g. to decide the
    sede) opened a `bypass_tenant_scope` there — TenantContextConflict, a 500
    on every inventory call made by a PIN-login employee.

    Not `request.state`: that is backed by `scope["state"]`, which the ASGI
    server may share between requests (asgi-lifespan hands every request the
    SAME dict) — the e2e harness then served the first caller's user to
    every later one.
    """
    scope = getattr(request, "scope", None)
    cached = scope.get(_USER_SCOPE_KEY) if isinstance(scope, dict) else None
    if isinstance(cached, dict):
        return cached
    user = await _resolve_current_user(request)
    if isinstance(scope, dict):
        scope[_USER_SCOPE_KEY] = user
    return user


_USER_SCOPE_KEY = "mesio.user"
_ORG_SCOPE_KEY = "mesio.org"


async def _resolve_current_user(request: Request) -> dict:
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
                # s.location_id — added to `staff` back in migration 0035
                # (NOT NULL) — was never selected here despite CLAUDE.md
                # documenting a "default_location_id" as already resolved:
                # that value is computed ONLY at login time (app/services/
                # auth.py) and handed to the frontend once; this per-request
                # auth dependency (used by get_current_user_scoped on every
                # authenticated call) was hard-coding location_id=None below
                # regardless, so no staff-scoped endpoint could ever actually
                # enforce "your own sede" from the JWT alone. Found and fixed
                # for chunk 4 (docs/claude/delivery-web.md) — the delivery
                # cashier endpoints are the first callers that need this to
                # be real. See memory/mesero-location-gap.md for the same gap
                # previously observed from the waiter-alerts side.
                query = """
                    SELECT s.org_id AS restaurant_id, s.location_id, s.role, s.roles,
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
                    "location_id": staff_member["location_id"],
                    "role": combined_role
                }

    user = await db.db_get_user(username)
    if user:
        return user

    raise HTTPException(status_code=401, detail="User not found")

# ── Sede (location) scoping ──────────────────────────────────────────────────
#
# PM decision 2026-09-20: an employee of one sede must never see another
# sede's data. Two role tiers decide what a caller may ask for:
#
#   owner / admin  — manage the whole business: may span every sede of their
#                    org, and may pick ONE with a header.
#   everyone else  — including `gerente`, who runs a single sede: pinned to
#                    their own `location_id`, whatever any header says.
#
# `staff_sections.ADMIN_ROLES` is the product-wide "admin" set and includes
# gerente (it grants every STAFF-APP section), so it is deliberately NOT the
# set used here — spanning sedes is a narrower privilege than seeing every
# section of your own.

SEDE_SPANNING_ROLES: frozenset[str] = frozenset({"owner", "admin"})


def roles_of(user: dict) -> set[str]:
    """The caller's normalized role set. `role` is a comma-joined string on
    both the `users` row and the staff dict built in get_current_user."""
    from app.services.staff_sections import normalize_role  # noqa: PLC0415

    return {
        normalize_role(r)
        for r in (user.get("role") or "").split(",")
        if r.strip()
    }


def may_span_locations(user: dict) -> bool:
    """True when the caller may see/choose any sede of their org."""
    return bool(roles_of(user) & SEDE_SPANNING_ROLES)


def resolve_sede_filter(
    request: Request,
    user: dict,
    *,
    admin_without_header: str = "all",
    allow_all_sentinel: bool = False,
) -> int | str | None:
    """The location_id a staff-facing listing must filter by, or None for
    "every sede of the org".

    This is the ONE place that decides it. Before it existed, a dozen routes
    each re-read `X-Branch-ID` (or `X-Location-ID`) with their own rules, and
    most of them applied NO role check at all: a cook could name another sede
    in a header, and a cook who named none saw every sede in the org.

    owner/admin: the header wins when it names a sede; with no header they get
    `admin_without_header` — "all" (None) for screens that have always been
    usable org-wide, or "own" for screens where a cross-sede view is
    meaningless and their own sede is the honest default.

    Everyone else: their own `location_id`, always. Raises 403 when they have
    none, because the alternative is showing them the whole org.

    Both header names are accepted — `X-Location-ID` is the Wave-1 name the
    newer surfaces use, `X-Branch-ID` the older one `mesioHeaders()` still
    sends — so callers do not have to care which one the frontend attached.

    `allow_all_sentinel` is for the stats/NPS family, whose repos
    distinguish "every sede of the org rolled up" (the string "all") from
    "no sede filter" (None). Only an admin can ever get the sentinel.
    """
    if may_span_locations(user):
        if allow_all_sentinel:
            raw_all = (request.headers.get("X-Branch-ID") or "").strip()
            if raw_all == "all":
                return "all"
            if raw_all == "matriz":
                return None
        for name in ("X-Location-ID", "X-Branch-ID"):
            raw = (request.headers.get(name) or "").strip()
            if raw.isdigit():
                return int(raw)
        if admin_without_header == "own":
            own = user.get("location_id")
            return int(own) if own else None
        return None

    own = user.get("location_id")
    if not own:
        raise HTTPException(
            status_code=403, detail="Tu usuario no tiene una sede asignada",
        )
    return int(own)


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

    Sede scoping (PM 2026-09-20: "los empleados de una sede no deben ver otra
    sede"): `X-Branch-ID` is honoured ONLY for owner/admin, who legitimately
    manage every sede of the org. It used to be honoured for ANY authenticated
    caller — a mesero, a cocinero or a cajero of sede A could put sede B's id
    in a header and this function would hand back sede B's restaurant row,
    which every downstream route then uses as its tenant + sede context. A
    `gerente` runs ONE sede (see staff_sections.ADMIN_ROLES and
    app/routes/location_delivery.py), so they are pinned to their own like any
    other employee. A mismatching header from such a caller is ignored rather
    than refused: the sede lives in the browser's localStorage and can go
    stale, and 403-ing every call of a waiter whose tablet remembers the wrong
    sede would take the floor down. It is logged so it stays visible.
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

    # 🛡️ MAGIA MULTI-SUCURSAL: only an owner/admin may switch sede from the
    # sidebar. X-Branch-ID ALWAYS carries a location_id, and the selected sede
    # must belong to the SAME org as the authenticated user — verified via
    # org_id. See the docstring for why a non-admin's header is dropped
    # instead of refused.
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        target_id = int(branch_header)
        if not may_span_locations(user):
            if location_id and int(location_id) != target_id:
                _log.warning(
                    "auth.sede_override_ignored",
                    username=user.get("username"),
                    own_location_id=int(location_id),
                    requested_location_id=target_id,
                )
            return default_rest

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
# restaurant in tenant_scope(). Most routes still use the original
# get_current_restaurant.
async def get_current_restaurant_scoped(request: Request):
    """Yield-based variant of get_current_restaurant that activates tenant_scope.

    Entering tenant_scope() pins
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


def require_plan_feature(feature: str) -> Callable:
    """FastAPI dependency: 403 with the upgrade copy when the caller's plan
    (trial included) does not include `feature` — see app/services/plans.py."""
    from app.services import plan_access  # noqa: PLC0415

    async def _check_plan(restaurant: dict = Depends(get_current_restaurant_scoped)) -> None:
        await plan_access.require_feature(int(restaurant["id"]), feature)

    return _check_plan


def require_module(module_name: str) -> Callable:
    """
    FastAPI dependency factory for module-level access control.

    Reads features directly from the already-loaded restaurant dict to avoid
    a second DB round-trip and normalisation mismatches in db_check_module.

    Opt-out (2026-10-01): a module is OFF only when the owner switched it off
    (False / "false"). It used to demand an explicit True, which no
    organization created since the plans existed has — Pro customers paid
    for reservations and got "módulo no activo". The plan is the real gate
    (require_plan_feature); this only honours an owner who turned it off,
    the same reading the bot and the sidebar already used.

    Raises:
        401 — if the Bearer token is missing or invalid (via get_current_restaurant).
        403 — if the owner switched the module off.
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
        switched_off = val is False or str(val).lower() == "false"
        if switched_off:
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

    The result is cached in the request's scope (see get_current_user for
    why not request.state) to avoid duplicate DB lookups when both
    get_current_org and get_current_location are used as dependencies in
    the same request.
    """
    cached = request.scope.get(_ORG_SCOPE_KEY)
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
            "features":        feats,
            "subscription_plan": r.get("subscription_plan", "restaurante"),
            "plan_code":       r.get("plan_code"),
            "comp_until":      r.get("comp_until"),
            "subscription_status": r.get("subscription_status", "active"),
        }

    request.scope[_ORG_SCOPE_KEY] = org
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