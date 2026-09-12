import json as _json
# Fix #12: passlib removed — bcrypt direct API via password_hash module (rounds=12 pinned)
from app.services.password_hash import hash_password, verify_password, is_legacy_hash
from app.services import database as db
from app.repositories import sessions_repo
from app.services.logging import get_logger

_log = get_logger(__name__)


# ── Org/Location shape helpers (Bloque S3) ────────────────────────────────────

def _loc_summary(loc: dict) -> dict:
    """Return a slim Location dict safe for embedding in login responses."""
    return {
        "id":               loc.get("id"),
        "name":             loc.get("name"),
        "is_primary":       loc.get("is_primary", False),
        "whatsapp_number":  loc.get("whatsapp_number"),
        "active":           loc.get("active", True),
    }


async def _build_org_shape(org: dict) -> dict:
    """Return the ``org`` sub-dict for login responses from an Org row."""
    feats = org.get("features") or {}
    if isinstance(feats, str):
        try:
            feats = _json.loads(feats)
        except Exception:
            feats = {}
    return {
        "id":                 org.get("id"),
        "name":               org.get("name"),
        "whatsapp_number":    org.get("whatsapp_number"),
        "features":           feats,
        "subscription_plan":  org.get("subscription_plan"),
        "locale":             feats.get("locale",   "es-CO"),
        "currency":           feats.get("currency", "COP"),
    }


# Pre-computed bcrypt hash used to equalize timing when a username does not
# exist (Fix #1 — login timing oracle mitigation). Generated with rounds=12.
_DUMMY_HASH = "$2b$12$CGrQL8okZSh9O2uDzZqkMu3hZLUK8vYoU1O./GGLu4ZzHMN3CrH/G"

# Unified error string — same for "user not found" and "wrong password" so
# callers cannot distinguish the two failure modes (Fix #1).
_INVALID_CREDS = "Credenciales inválidas"


async def login(username: str, password: str) -> dict:
    # ── Intento 1: tabla users (admin / gerente / owner) ──────────────────────
    user = await db.db_get_user(username)
    if not user:
        # ── Intento 2: tabla staff (operativos con contraseña) ────────────────
        candidates = await db.db_get_staff_candidates_by_name(username)
        member = next((c for c in candidates if verify_password(password, c["pin"], c.get("name", ""))), None)
        if not member:
            # Equalise timing: run a dummy bcrypt verify so "user not found"
            # takes the same wall time as "wrong password" (Fix #1).
            verify_password(password, _DUMMY_HASH)
            return {"success": False, "error": _INVALID_CREDS}

        # Opportunistic legacy → bcrypt upgrade. We hold the plaintext only
        # here; rehash + persist before continuing. Failure to upgrade must
        # NOT block the login (best-effort, logged for observability).
        if is_legacy_hash(member.get("pin", "")) and member.get("org_id"):
            try:
                from app.repositories.staff_repo import db_update_staff_pin_hash  # noqa: PLC0415
                await db_update_staff_pin_hash(
                    str(member["id"]), int(member["org_id"]), hash_password(password)
                )
                _log.info("auth.password.legacy_upgraded", scope="staff", staff_id=str(member["id"]))
            except Exception:
                _log.exception("auth.password.legacy_upgrade_failed", scope="staff", staff_id=str(member.get("id")))

        token = await sessions_repo.create_session(f"staff:{member['id']}")

        roles     = member.get("roles") or [member.get("role", "mesero")]
        role      = ",".join(roles)
        branch_id = member.get("restaurant_id")
        whatsapp_number = ""
        features: dict = {}
        restaurant_name = ""

        # ── New shape: resolve Org + Location post-0037 (Wave 2) ─────────────────
        org_shape: dict | None = None
        locations_list: list = []
        default_location_id: int | None = None

        try:
            if branch_id:
                # branch_id here is member["restaurant_id"] == staff.org_id
                # (confirmed: db_get_staff_for_pin_login queries
                # `staff WHERE org_id=$1`) — always an org_id, safe.
                restaurant = await db.db_get_restaurant_by_org_id(branch_id)
                if restaurant:
                    restaurant_name = restaurant.get("name", "")
                    whatsapp_number = restaurant.get("whatsapp_number", "")
                    raw = restaurant.get("features") or {}
                    features = _json.loads(raw) if isinstance(raw, str) else dict(raw)

                # Resolve Org/Location via locations table (post-0037, no mapping table)
                from app.repositories.restaurant_repo import (  # noqa: PLC0415
                    db_get_org_by_id,
                    db_get_org_locations,
                )

                loc = await db_get_location_by_id_safe(int(branch_id))
                if loc and loc.get("org_id"):
                    org_id_resolved = int(loc["org_id"])
                else:
                    _log.warning("auth.org_id_fallback_used", branch_id=int(branch_id))
                    org_id_resolved = int(branch_id)  # Matriz invariant fallback

                org_obj = await db_get_org_by_id(org_id_resolved)
                if org_obj:
                    org_shape = await _build_org_shape(org_obj)
                # For staff: only the Location they belong to
                if loc:
                    locations_list = [_loc_summary(loc)]
                    default_location_id = loc["id"]

        except Exception:
            _log.exception("auth.staff_login.org_resolve_error")

        if branch_id and org_shape is None:
            _log.error("auth.login.org_resolve_failed_hard", branch_id=branch_id, username=member.get("name", ""))
            return {"success": False, "error": "Problema con la configuración de la sucursal"}

        legacy_restaurant = {
            "id":               branch_id,
            "name":             restaurant_name,
            "username":         member["name"],
            "role":             role,
            "branch_id":        branch_id,
            "whatsapp_number":  whatsapp_number,
            "features":         features,
            "locale":           features.get("locale",   "es-CO"),
            "currency":         features.get("currency", "COP"),
        }

        response: dict = {
            "success":  True,
            "token":    token,
            "role":     role,
            "staff_id": member["id"],
            "restaurant": legacy_restaurant,  # legacy key — kept for backward compat
        }
        if org_shape is not None:
            response["org"] = org_shape
            response["locations"] = locations_list
            response["default_location_id"] = default_location_id
        return response

    if not verify_password(password, user["password_hash"], username):
        return {"success": False, "error": _INVALID_CREDS}

    # Opportunistic legacy → bcrypt upgrade for admin/owner users.
    # Best-effort: rehash failure must not block the login.
    if is_legacy_hash(user.get("password_hash", "")):
        try:
            await db.db_update_user_password(username, hash_password(password))
            _log.info("auth.password.legacy_upgraded", scope="user", username_prefix=(username[:3] + "***"))
        except Exception:
            _log.exception("auth.password.legacy_upgrade_failed", scope="user")

    token = await sessions_repo.create_session(username.lower().strip())

    role = user.get("role", "owner")
    whatsapp_number = ""
    features: dict = {}

    # ── New shape: resolve Org + Locations post-0037 (Wave 2) ────────────────
    org_shape: dict | None = None
    locations_list: list = []
    default_location_id: int | None = None

    # P0 fix (2026-09): resolve ONLY through the explicit users.org_id /
    # users.location_id columns (backfilled by the users_org_location
    # migration). The old path resolved via the mixed-kind users.branch_id
    # column — some writers stored an org_id there, others a location_id —
    # feeding it into the now-deleted ambiguous db_get_restaurant_by_id lookup
    # and, when that failed, into a "Matriz invariant" fallback that treated
    # branch_id as an org_id regardless. Either path could silently resolve
    # to an UNRELATED org whenever branch_id collided with someone else's
    # real id. A user whose org_id cannot be resolved is DENIED here — never
    # guessed via name-match or branch_id fallback.
    org_id = user.get("org_id")
    location_id = user.get("location_id")

    if not org_id:
        _log.error("auth.login.no_tenant_context", username=username,
                   restaurant_name=user.get("restaurant_name"))
        return {"success": False, "error": "Problema con la configuración de la sucursal"}

    try:
        from app.repositories.restaurant_repo import (  # noqa: PLC0415
            db_get_org_by_id,
            db_get_org_locations,
        )

        restaurant = await db.db_get_restaurant_by_org_id(int(org_id))
        if restaurant:
            whatsapp_number = restaurant.get("whatsapp_number", "")
            raw = restaurant.get("features") or {}
            features = _json.loads(raw) if isinstance(raw, str) else dict(raw)

        loc = await db_get_location_by_id_safe(int(location_id)) if location_id else None
        if loc and int(loc.get("org_id") or -1) != int(org_id):
            # Ownership sanity check: never trust a location_id that does
            # not belong to this user's own org.
            _log.warning(
                "auth.login.location_org_mismatch",
                username=username, location_id=location_id, org_id=org_id,
            )
            loc = None

        org_obj = await db_get_org_by_id(int(org_id))
        if org_obj:
            org_shape = await _build_org_shape(org_obj)
            all_locs = await db_get_org_locations(int(org_id))
            locations_list = [_loc_summary(l) for l in all_locs]
            # Post-Wave-2: no primary flag — prefer the user's own assigned
            # location, else the lowest id as deterministic default.
            default_location_id = loc["id"] if loc else (all_locs[0]["id"] if all_locs else None)

    except Exception:
        _log.exception("auth.admin_login.org_resolve_error")

    if org_shape is None:
        _log.error("auth.login.org_resolve_failed_hard", org_id=org_id, username=username)
        return {"success": False, "error": "Problema con la configuración de la sucursal"}

    legacy_restaurant = {
        "id": org_id,
        "name": user["restaurant_name"],
        "username": username,
        "role": role,
        "branch_id": org_id,
        "whatsapp_number": whatsapp_number,
        "features": features,
        "locale":   features.get("locale",   "es-CO"),
        "currency": features.get("currency", "COP"),
    }

    response: dict = {
        "success": True,
        "token": token,
        "role": role,
        "restaurant": legacy_restaurant,  # legacy key — kept for backward compat
    }
    if org_shape is not None:
        response["org"] = org_shape
        response["locations"] = locations_list
        response["default_location_id"] = default_location_id
    return response


async def db_get_location_by_id_safe(location_id: int) -> dict | None:
    """Thin helper used inside auth.py to avoid a circular import in the common case."""
    from app.repositories.restaurant_repo import db_get_location_by_id  # noqa: PLC0415
    return await db_get_location_by_id(location_id)

async def verify_token(token: str) -> str | None:
    return await sessions_repo.get_session(token)

async def logout(token: str):
    await sessions_repo.delete_session(token)

async def create_user(username: str, password: str, restaurant_name: str) -> dict:
    success = await db.db_create_user(username, hash_password(password), restaurant_name)
    if not success:
        return {"success": False, "error": "Usuario ya existe"}
    return {"success": True, "message": f"Usuario {username} creado"}

async def get_users() -> list:
    return await db.db_get_all_users()
