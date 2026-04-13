"""
setup_demo.py — Creates a complete demo restaurant ready for a live Mesio demo.

Usage:
    python scripts/setup_demo.py

Reads DATABASE_URL from environment (required).
Reads DEMO_WHATSAPP_NUMBER from environment or prompts interactively.

Idempotent: safe to run multiple times.
"""
import asyncio
import json
import os
import sys

import asyncpg
from passlib.hash import bcrypt

# ── Demo data ─────────────────────────────────────────────────────────────────

DEMO_RESTAURANT_NAME = "Restaurante Demo Mesio"
DEMO_ADDRESS = "Calle 100 #15-20, Bogotá"

DEMO_MENU = {
    "categories": [
        {
            "name": "Entradas",
            "items": [
                {
                    "name": "Empanadas",
                    "description": "Empanadas de pipián y carne con ají de maní",
                    "price": 8000,
                    "sku": "ENT-001",
                    "available": True,
                },
                {
                    "name": "Patacones con hogao",
                    "description": "Patacones fritos con hogao casero y suero costeño",
                    "price": 12000,
                    "sku": "ENT-002",
                    "available": True,
                },
                {
                    "name": "Ceviche de camarón",
                    "description": "Camarón fresco marinado en limón, cilantro y tomate",
                    "price": 18000,
                    "sku": "ENT-003",
                    "available": True,
                },
            ],
        },
        {
            "name": "Platos Fuertes",
            "items": [
                {
                    "name": "Bandeja Paisa",
                    "description": "Frijoles, chicharrón, carne molida, chorizo, morcilla, arepa, arroz y huevo",
                    "price": 28000,
                    "sku": "PF-001",
                    "available": True,
                },
                {
                    "name": "Ajiaco Santafereño",
                    "description": "Sopa bogotana con tres tipos de papa, pollo, guasca y crema de leche",
                    "price": 22000,
                    "sku": "PF-002",
                    "available": True,
                },
                {
                    "name": "Pescado frito con arroz de coco",
                    "description": "Mojarra roja frita con arroz de coco y patacón",
                    "price": 25000,
                    "sku": "PF-003",
                    "available": True,
                },
                {
                    "name": "Lomo de res en salsa de champiñones",
                    "description": "Lomo fino a la plancha con salsa cremosa de champiñones y papas al vapor",
                    "price": 32000,
                    "sku": "PF-004",
                    "available": True,
                },
            ],
        },
        {
            "name": "Bebidas",
            "items": [
                {
                    "name": "Limonada natural",
                    "description": "Limonada fría con panela o azúcar",
                    "price": 6000,
                    "sku": "BEB-001",
                    "available": True,
                },
                {
                    "name": "Jugo de lulo",
                    "description": "Jugo natural de lulo colombiano en agua o leche",
                    "price": 7000,
                    "sku": "BEB-002",
                    "available": True,
                },
                {
                    "name": "Agua",
                    "description": "Agua mineral o del tiempo",
                    "price": 4000,
                    "sku": "BEB-003",
                    "available": True,
                },
                {
                    "name": "Cerveza Club Colombia",
                    "description": "Botella 330ml fría",
                    "price": 8000,
                    "sku": "BEB-004",
                    "available": True,
                },
            ],
        },
        {
            "name": "Postres",
            "items": [
                {
                    "name": "Tres leches",
                    "description": "Bizcocho esponjoso bañado en tres tipos de leche con crema chantilly",
                    "price": 12000,
                    "sku": "POS-001",
                    "available": True,
                },
                {
                    "name": "Oblea con arequipe",
                    "description": "Oblea crujiente con arequipe, mermelada y coco rallado",
                    "price": 8000,
                    "sku": "POS-002",
                    "available": True,
                },
            ],
        },
    ]
}

DEMO_FEATURES = {
    "payment_methods": [
        {"name": "Efectivo", "enabled": True},
        {"name": "Nequi", "enabled": True},
        {"name": "Daviplata", "enabled": True},
        {"name": "Transferencia Bancaria", "enabled": True},
    ],
    "payment_instructions": {
        "Nequi": "Enviar al 300-123-4567",
        "Daviplata": "Enviar al 300-123-4567",
        "Transferencia Bancaria": "Banco de Bogotá - Cuenta de Ahorros 123456789",
    },
    "bot_active": True,
    "domicilio_active": True,
    "recoger_active": True,
    "upsell_active": True,
    "currency": "COP",
    "locale": "es-CO",
    "timezone": "America/Bogota",
}

DEMO_USER_EMAIL = "demo@mesio.co"
DEMO_USER_PASSWORD = "demo2024"
DEMO_USER_NAME = "Demo Owner"
DEMO_USER_ROLE = "owner"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_phone(number: str) -> str:
    """Strip non-digit characters except leading +."""
    cleaned = "".join(c for c in number if c.isdigit() or c == "+")
    return cleaned


def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    # asyncpg requires 'postgresql://' not 'postgres://'
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _get_whatsapp_number() -> str:
    number = os.environ.get("DEMO_WHATSAPP_NUMBER", "").strip()
    if not number:
        number = input("Enter the demo WhatsApp number (e.g. +573001234567): ").strip()
    if not number:
        print("ERROR: WhatsApp number is required.")
        sys.exit(1)
    return _normalize_phone(number)


# ── Core logic ────────────────────────────────────────────────────────────────

async def setup_demo(conn: asyncpg.Connection, whatsapp_number: str) -> None:
    # ── 1. Restaurant ──────────────────────────────────────────────────────────
    existing_restaurant = await conn.fetchrow(
        "SELECT id, name FROM restaurants WHERE whatsapp_number = $1",
        whatsapp_number,
    )

    if existing_restaurant:
        restaurant_id = existing_restaurant["id"]
        print(f"  [SKIP] Restaurant already exists (id={restaurant_id}, name='{existing_restaurant['name']}')")
    else:
        # asyncpg encodes dict → JSONB natively; cast ::jsonb is safe
        await conn.execute(
            """
            INSERT INTO restaurants (name, whatsapp_number, address, menu, features)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            """,
            DEMO_RESTAURANT_NAME,
            whatsapp_number,
            DEMO_ADDRESS,
            json.dumps(DEMO_MENU),
            json.dumps(DEMO_FEATURES),
        )
        restaurant_id = await conn.fetchval(
            "SELECT id FROM restaurants WHERE whatsapp_number = $1", whatsapp_number
        )
        print(f"  [OK]   Restaurant created: '{DEMO_RESTAURANT_NAME}' (id={restaurant_id})")

    # ── 2. Owner user ──────────────────────────────────────────────────────────
    existing_user = await conn.fetchrow(
        "SELECT username FROM users WHERE username = $1",
        DEMO_USER_EMAIL.lower().strip(),
    )

    if existing_user:
        print(f"  [SKIP] User already exists: {DEMO_USER_EMAIL}")
    else:
        password_hash = bcrypt.hash(DEMO_USER_PASSWORD)
        try:
            await conn.execute(
                """
                INSERT INTO users (username, password_hash, restaurant_name, role, branch_id)
                VALUES ($1, $2, $3, $4, $5)
                """,
                DEMO_USER_EMAIL.lower().strip(),
                password_hash,
                DEMO_RESTAURANT_NAME,
                DEMO_USER_ROLE,
                restaurant_id,
            )
            print(f"  [OK]   User created: {DEMO_USER_EMAIL}")
        except asyncpg.UniqueViolationError:
            print(f"  [SKIP] User already exists (race): {DEMO_USER_EMAIL}")


async def main() -> None:
    print("=" * 60)
    print("  Mesio — Demo Restaurant Setup")
    print("=" * 60)

    database_url = _get_database_url()
    whatsapp_number = _get_whatsapp_number()

    print(f"\n  Connecting to database...")
    conn = await asyncpg.connect(database_url)
    try:
        print(f"  WhatsApp number : {whatsapp_number}")
        print()
        await setup_demo(conn, whatsapp_number)
    finally:
        await conn.close()

    print()
    print("=" * 60)
    print("  Setup complete. Next steps:")
    print()
    print("  Dashboard login:")
    print(f"    URL      : http://localhost:8000/dashboard")
    print(f"    Email    : {DEMO_USER_EMAIL}")
    print(f"    Password : {DEMO_USER_PASSWORD}")
    print()
    print("  WhatsApp bot:")
    print(f"    Number   : {whatsapp_number}")
    print(f"    Endpoint : http://localhost:8000/webhook")
    print()
    print("  To reset: delete the restaurant row (CASCADE removes the user link)")
    print("    DELETE FROM restaurants WHERE whatsapp_number = '<number>';")
    print("    DELETE FROM users WHERE username = 'demo@mesio.co';")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
