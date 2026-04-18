import json as _json
from passlib.context import CryptContext
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

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str, _username: str = "") -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        # Fallback para usuarios viejos con sha256 (legacy hash — not bcrypt).
        # Log as warning so we can measure how many legacy users remain before
        # dropping the fallback.  Username is obfuscated to 3 chars for privacy.
        import hashlib
        result = hashlib.sha256(plain_password.encode()).hexdigest() == hashed_password
        if result:
            obfuscated = (_username[:3] + "***") if _username else "***"
            _log.warning(
                "auth.password.sha256_fallback_used",
                username_prefix=obfuscated,
            )
        return result

async def login(username: str, password: str) -> dict:
    # ── Intento 1: tabla users (admin / gerente / owner) ──────────────────────
    user = await db.db_get_user(username)
    if not user:
        # ── Intento 2: tabla staff (operativos con contraseña) ────────────────
        candidates = await db.db_get_staff_candidates_by_name(username)
        member = next((c for c in candidates if verify_password(password, c["pin"], c.get("name", ""))), None)
        if not member:
            return {"success": False, "error": "Usuario o contraseña incorrectos"}

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
                restaurant = await db.db_get_restaurant_by_id(branch_id)
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
        return {"success": False, "error": "Contraseña incorrecta"}

    token = await sessions_repo.create_session(username.lower().strip())

    role = user.get("role", "owner")
    branch_id = user.get("branch_id")
    whatsapp_number = ""
    features: dict = {}

    # ── New shape: resolve Org + Locations post-0037 (Wave 2) ────────────────
    org_shape: dict | None = None
    locations_list: list = []
    default_location_id: int | None = None

    try:
        if branch_id:
            restaurant = await db.db_get_restaurant_by_id(branch_id)
            if restaurant:
                whatsapp_number = restaurant.get("whatsapp_number", "")
                raw = restaurant.get("features") or {}
                features = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        else:
            all_restaurants = await db.db_get_all_restaurants()
            for r in all_restaurants:
                if r["name"].lower().strip() == user["restaurant_name"].lower().strip():
                    whatsapp_number = r.get("whatsapp_number", "")
                    branch_id = r.get("id")
                    raw = r.get("features") or {}
                    features = _json.loads(raw) if isinstance(raw, str) else dict(raw)
                    break
            if not whatsapp_number and all_restaurants:
                whatsapp_number = all_restaurants[0].get("whatsapp_number", "")
                branch_id = all_restaurants[0].get("id")
                raw = all_restaurants[0].get("features") or {}
                features = _json.loads(raw) if isinstance(raw, str) else dict(raw)

        # Resolve Org via locations table (post-0037, no mapping table)
        if branch_id:
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
                all_locs = await db_get_org_locations(org_id_resolved)
                locations_list = [_loc_summary(l) for l in all_locs]
                primary = next((l for l in all_locs if l.get("is_primary")), None)
                default_location_id = primary["id"] if primary else (all_locs[0]["id"] if all_locs else None)

    except Exception:
        _log.exception("auth.admin_login.org_resolve_error")

    if branch_id and org_shape is None:
        _log.error("auth.login.org_resolve_failed_hard", branch_id=branch_id, username=username)
        return {"success": False, "error": "Problema con la configuración de la sucursal"}

    legacy_restaurant = {
        "id": branch_id,
        "name": user["restaurant_name"],
        "username": username,
        "role": role,
        "branch_id": branch_id,
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