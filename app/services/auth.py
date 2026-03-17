import hashlib
import secrets
from app.services import database as db

active_tokens: dict = {}

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

async def login(username: str, password: str) -> dict:
    user = await db.db_get_user(username)
    if not user:
        return {"success": False, "error": "Usuario no encontrado"}
    if user["password_hash"] != hash_password(password):
        return {"success": False, "error": "Contrasena incorrecta"}

    token = secrets.token_hex(32)
    active_tokens[token] = username.lower().strip()

    role = user.get("role", "owner")
    branch_id = user.get("branch_id")

    whatsapp_number = ""
    try:
        if branch_id:
            restaurant = await db.db_get_restaurant_by_id(branch_id)
            if restaurant:
                whatsapp_number = restaurant.get("whatsapp_number", "")
        else:
            all_restaurants = await db.db_get_all_restaurants()
            for r in all_restaurants:
                if r["name"].lower().strip() == user["restaurant_name"].lower().strip():
                    whatsapp_number = r.get("whatsapp_number", "")
                    branch_id = r.get("id")
                    break
            if not whatsapp_number and all_restaurants:
                whatsapp_number = all_restaurants[0].get("whatsapp_number", "")
                branch_id = all_restaurants[0].get("id")
    except Exception as e:
        print(f"Warning login whatsapp_number: {e}")

    return {
        "success": True,
        "token": token,
        "restaurant": {
            "name": user["restaurant_name"],
            "username": username,
            "role": role,
            "branch_id": branch_id,
            "whatsapp_number": whatsapp_number,
        },
    }

def verify_token(token: str) -> str | None:
    return active_tokens.get(token)

def logout(token: str):
    active_tokens.pop(token, None)

async def create_user(username: str, password: str, restaurant_name: str) -> dict:
    success = await db.db_create_user(username, hash_password(password), restaurant_name)
    if not success:
        return {"success": False, "error": "Usuario ya existe"}
    return {"success": True, "message": f"Usuario {username} creado"}

async def get_users() -> list:
    return await db.db_get_all_users()
