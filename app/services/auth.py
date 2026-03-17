import hashlib
import secrets
from app.services import database as db

active_tokens: dict = {}  # token → username (en memoria está bien, se regenera al login)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


async def login(username: str, password: str) -> dict:
    user = await db.db_get_user(username)
    if not user:
        return {"success": False, "error": "Usuario no encontrado"}
    if user["password_hash"] != hash_password(password):
        return {"success": False, "error": "Contraseña incorrecta"}

    token = secrets.token_hex(32)
    active_tokens[token] = username.lower().strip()

    role = user.get("role", "owner")
    return {
        "success": True,
        "token": token,
        "restaurant": {
            "name": user["restaurant_name"],
            "username": username,
            "role": role,
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
