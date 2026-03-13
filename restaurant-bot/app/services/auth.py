import hashlib
import secrets
import os
from datetime import datetime

# Usuarios en memoria — en producción usar base de datos
# Formato: { username: { password_hash, restaurant_name, token } }
users: dict = {
    "demo@restaurante.com": {
        "password_hash": hashlib.sha256("demo123".encode()).hexdigest(),
        "restaurant_name": "La Trattoria Italiana",
        "token": None
    }
}

active_tokens: dict = {}  # token → username


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def login(username: str, password: str) -> dict:
    username = username.lower().strip()
    if username not in users:
        return {"success": False, "error": "Usuario no encontrado"}

    user = users[username]
    if user["password_hash"] != hash_password(password):
        return {"success": False, "error": "Contraseña incorrecta"}

    token = secrets.token_hex(32)
    active_tokens[token] = username
    users[username]["token"] = token

    return {
        "success": True,
        "token": token,
        "restaurant": {
            "name": user["restaurant_name"],
            "username": username
        }
    }


def verify_token(token: str) -> str | None:
    return active_tokens.get(token)


def logout(token: str):
    if token in active_tokens:
        username = active_tokens[token]
        del active_tokens[token]
        if username in users:
            users[username]["token"] = None


def create_user(username: str, password: str, restaurant_name: str) -> dict:
    username = username.lower().strip()
    if username in users:
        return {"success": False, "error": "Usuario ya existe"}

    users[username] = {
        "password_hash": hash_password(password),
        "restaurant_name": restaurant_name,
        "token": None
    }
    return {"success": True, "message": f"Usuario {username} creado"}


def get_users() -> list:
    return [{"username": u, "restaurant": d["restaurant_name"]} for u, d in users.items()]
