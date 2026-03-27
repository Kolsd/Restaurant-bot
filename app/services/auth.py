import json as _json
import secrets
from passlib.context import CryptContext
from app.services import database as db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        # Fallback para usuarios viejos con sha256
        import hashlib
        return hashlib.sha256(plain_password.encode()).hexdigest() == hashed_password

async def login(username: str, password: str) -> dict:
    # ── Intento 1: tabla users (admin / gerente / owner) ──────────────────────
    user = await db.db_get_user(username)
    if not user:
        # ── Intento 2: tabla staff (operativos con contraseña) ────────────────
        candidates = await db.db_get_staff_candidates_by_name(username)
        member = next((c for c in candidates if verify_password(password, c["pin"])), None)
        if not member:
            return {"success": False, "error": "Usuario o contraseña incorrectos"}

        token = secrets.token_hex(32)
        await db.db_save_session(token, f"staff:{member['id']}")

        roles     = member.get("roles") or [member.get("role", "mesero")]
        role      = ",".join(roles)
        branch_id = member.get("restaurant_id")
        whatsapp_number = ""
        features: dict = {}
        try:
            if branch_id:
                restaurant = await db.db_get_restaurant_by_id(branch_id)
                if restaurant:
                    whatsapp_number = restaurant.get("whatsapp_number", "")
                    raw = restaurant.get("features") or {}
                    features = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception as e:
            print(f"Warning login staff: {e}")

        return {
            "success":  True,
            "token":    token,
            "role":     role,
            "staff_id": member["id"],
            "restaurant": {
                "name":             member["name"],
                "username":         member["name"],
                "role":             role,
                "branch_id":        branch_id,
                "whatsapp_number":  whatsapp_number,
                "features":         features,
                "locale":           features.get("locale",   "es-CO"),
                "currency":         features.get("currency", "COP"),
            },
        }

    if not verify_password(password, user["password_hash"]):
        return {"success": False, "error": "Contraseña incorrecta"}

    token = secrets.token_hex(32)
    await db.db_save_session(token, username.lower().strip())

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
    except Exception as e:
        print(f"Warning login: {e}")

    return {
        "success": True,
        "token": token,
        "role": role,
        "restaurant": {
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
    return await db.db_get_session(token)

async def logout(token: str):
    await db.db_delete_session(token)

async def create_user(username: str, password: str, restaurant_name: str) -> dict:
    success = await db.db_create_user(username, hash_password(password), restaurant_name)
    if not success:
        return {"success": False, "error": "Usuario ya existe"}
    return {"success": True, "message": f"Usuario {username} creado"}

async def get_users() -> list:
    return await db.db_get_all_users()