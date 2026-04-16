import json as _json
from passlib.context import CryptContext
from app.services import database as db
from app.repositories import sessions_repo
from app.services.logging import get_logger

_log = get_logger(__name__)

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
        try:
            if branch_id:
                restaurant = await db.db_get_restaurant_by_id(branch_id)
                if restaurant:
                    restaurant_name = restaurant.get("name", "")
                    whatsapp_number = restaurant.get("whatsapp_number", "")
                    raw = restaurant.get("features") or {}
                    features = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            from app.services.logging import get_logger as _get_log  # noqa: PLC0415
            _get_log(__name__).exception("auth.staff_login.features_parse_error")

        return {
            "success":  True,
            "token":    token,
            "role":     role,
            "staff_id": member["id"],
            "restaurant": {
                "id":               branch_id,
                "name":             restaurant_name,
                "username":         member["name"],
                "role":             role,
                "branch_id":        branch_id,
                "whatsapp_number":  whatsapp_number,
                "features":         features,
                "locale":           features.get("locale",   "es-CO"),
                "currency":         features.get("currency", "COP"),
            },
        }

    if not verify_password(password, user["password_hash"], username):
        return {"success": False, "error": "Contraseña incorrecta"}

    token = await sessions_repo.create_session(username.lower().strip())

    role = user.get("role", "owner")
    branch_id = user.get("branch_id")
    whatsapp_number = ""
    features: dict = {}
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
    except Exception:
        from app.services.logging import get_logger as _get_log  # noqa: PLC0415
        _get_log(__name__).exception("auth.admin_login.restaurant_resolve_error")

    return {
        "success": True,
        "token": token,
        "role": role,
        "restaurant": {
            "id": branch_id,
            "name": user["restaurant_name"],
            "username": username,
            "role": role,
            "branch_id": branch_id,
            "whatsapp_number": whatsapp_number,
            "features": features,
            "locale":   features.get("locale",   "es-CO"),
            "currency": features.get("currency", "COP"),
        },
    }

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