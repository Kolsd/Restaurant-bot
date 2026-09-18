"""
scripts/dev/seed_delivery_demo.py — Local-only seed for manually verifying
the delivery/pickup ordering page `/pedir/{slug}` (docs/claude/delivery-web.md
chunk 5) in a browser. Follows the style of scripts/dev/seed_staff_app_demo.py.

Creates, idempotently:
  - One organization with a known, stable slug and a real menu, including
    ONE dish deliberately marked sold out (menu_availability.available=false)
    so the "Agotado" / server-side refusal path can be exercised manually.
  - Two locations ("sedes") in Bogotá a few km apart, each with GPS
    coordinates and a `delivery_config` (delivery + pickup on, a fee, a
    minimum order, a coverage radius, cash + transfer payment methods).
    `opening_hours` is left at its column default (`{}`) — is_location_open()
    treats "no configuration at all" as OPEN (see app/services/delivery.py),
    which is the simplest way to guarantee both sedes read as open right now
    without hardcoding a schedule that could go stale.
  - One cashier ("caja") and one courier ("domiciliario") staff member, for
    the cashier "Domicilios" section and the rider flow built in later chunks.

NEVER run this against a production database. Intended DB: the local
unit-test database (mesio_tests) or a disposable scratch DB (mesio_fresh),
same convention as seed_staff_app_demo.py.

Usage:
    DATABASE_URL=postgresql://postgres:mesio_local_dev@localhost:5432/mesio_tests \\
    .venv/Scripts/python.exe scripts/dev/seed_delivery_demo.py

Idempotent: safe to run multiple times (upserts by org slug / unique
whatsapp_number, and by (org_id, name) for locations/staff/dishes). Refuses
to run if DATABASE_URL is unset, or looks like a remote/production database.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg
from passlib.hash import bcrypt as pin_bcrypt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ORG_NAME = "Mesio Delivery Demo"
ORG_SLUG = "delivery-demo"
BOT_NUMBER = "+570DELIVERYDEMO"

# Two sedes in Bogotá, ~4.5 km apart — close enough that a GPS point near
# either one exercises the "nearest open covering sede wins" ladder, far
# enough that a point near one is clearly outside the other's radius_km.
SEDE_CENTRO = {
    "name": "Sede Centro",
    "address": "Cra 7 # 20-30, Bogotá",
    "phone": "3011234567",
    "lat": 4.6097,
    "lon": -74.0817,
}
SEDE_CHAPINERO = {
    "name": "Sede Chapinero",
    "address": "Cl 63 # 11-45, Bogotá",
    "phone": "3017654321",
    "lat": 4.6497,
    "lon": -74.0630,
}

DELIVERY_CONFIG = {
    "delivery_enabled": True,
    "pickup_enabled": True,
    "delivery_fee": 5000,
    "min_order": 20000,
    "radius_km": 5,
    "prep_minutes": 25,
    "payment_methods": ["efectivo", "nequi", "tarjeta"],
}

MENU = {
    "Platos": [
        {"name": "Bandeja Paisa", "description": "Plato típico", "price": 32000, "active": True},
        {"name": "Sancocho de Gallina", "description": "Sopa tradicional", "price": 24000, "active": True},
        {"name": "Ajiaco Santafereño", "description": "Sopa con pollo, papa criolla y mazorca", "price": 26000, "active": True},
    ],
    "Bebidas": [
        {"name": "Limonada de Coco", "description": "Limonada natural con coco", "price": 8000, "active": True},
        {"name": "Jugo de Mora", "description": "Jugo natural de mora", "price": 7000, "active": True},
    ],
}

# Deliberately sold out so the "Agotado" card state + the server-side
# dish_sold_out checkout refusal (docs/claude/delivery-web.md chunk 4) can
# both be exercised manually from /pedir/{slug}.
SOLD_OUT_DISH = "Ajiaco Santafereño"

FEATURES = {
    "bot_active": True,
    "domicilio_active": True,
    "recoger_active": True,
    "currency": "COP",
    "locale": "es-CO",
    "timezone": "America/Bogota",
}

CASHIER_NAME = "Camila Torres"
CASHIER_PIN = "3333"
COURIER_NAME = "Julián Restrepo"
COURIER_PIN = "4444"


def _forbid_prod(database_url: str) -> None:
    lowered = database_url.lower()
    banned = ("railway", "amazonaws", "render.com", "supabase", "prod")
    if any(b in lowered for b in banned):
        print(f"REFUSING to run: DATABASE_URL looks like a remote/production DB ({database_url!r}).")
        print("This script is for a local dev/test database only.")
        sys.exit(1)
    if "localhost" not in lowered and "127.0.0.1" not in lowered:
        print(f"REFUSING to run: DATABASE_URL is not localhost ({database_url!r}). Local DB only.")
        sys.exit(1)


async def _seed_location(conn: asyncpg.Connection, org_id: int, sede: dict) -> int:
    existing = await conn.fetchrow(
        "SELECT id FROM locations WHERE org_id = $1 AND name = $2", org_id, sede["name"],
    )
    if existing:
        location_id = existing["id"]
        await conn.execute(
            """
            UPDATE locations
            SET address = $2, phone = $3, latitude = $4, longitude = $5,
                delivery_config = $6::jsonb, active = true, updated_at = NOW()
            WHERE id = $1
            """,
            location_id, sede["address"], sede["phone"], sede["lat"], sede["lon"],
            json.dumps(DELIVERY_CONFIG),
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO locations
                (org_id, name, code, address, phone, latitude, longitude,
                 active, timezone, delivery_config)
            VALUES ($1, $2, $3, $4, $5, $6, $7, true, 'America/Bogota', $8::jsonb)
            RETURNING id
            """,
            org_id, sede["name"], sede["name"].lower().replace(" ", "-"),
            sede["address"], sede["phone"], sede["lat"], sede["lon"],
            json.dumps(DELIVERY_CONFIG),
        )
        location_id = row["id"]
    return location_id


async def _seed_staff(conn: asyncpg.Connection, org_id: int, name: str, username: str, role: str, pin: str) -> None:
    pin_hash = pin_bcrypt.hash(pin)
    existing = await conn.fetchrow("SELECT id FROM staff WHERE org_id = $1 AND name = $2", org_id, name)
    roles_json = json.dumps([role])
    if existing:
        await conn.execute(
            "UPDATE staff SET pin = $2, role = $3, roles = $4::jsonb, active = true WHERE id = $1",
            existing["id"], pin_hash, role, roles_json,
        )
    else:
        await conn.execute(
            """INSERT INTO staff (org_id, name, username, role, pin, phone, roles, document_number)
               VALUES ($1, $2, $3, $4, $5, '', $6::jsonb, '')""",
            org_id, name, username, role, pin_hash, roles_json,
        )


async def seed(conn: asyncpg.Connection) -> dict:
    async with conn.transaction():
        org_row = await conn.fetchrow(
            """
            INSERT INTO organizations (name, slug, whatsapp_number, menu, features)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            ON CONFLICT (whatsapp_number) WHERE whatsapp_number IS NOT NULL
              DO UPDATE SET name = EXCLUDED.name, slug = EXCLUDED.slug,
                            menu = EXCLUDED.menu, features = EXCLUDED.features
            RETURNING id
            """,
            ORG_NAME, ORG_SLUG, BOT_NUMBER, json.dumps(MENU), json.dumps(FEATURES),
        )
        org_id = org_row["id"]

        centro_id = await _seed_location(conn, org_id, SEDE_CENTRO)
        chapinero_id = await _seed_location(conn, org_id, SEDE_CHAPINERO)

        # ── Sold-out dish + staff: RLS-FORCEd tables, need app.org_id set
        # even for the postgres superuser connection's own sanity (mirrors
        # the mesio_app convention in seed_staff_app_demo.py; postgres
        # bypasses RLS regardless, this just keeps the pattern consistent
        # for anyone copying this script against a non-superuser DSN later).
        await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))

        # NOT `ON CONFLICT (dish_name, org_id)` — the real menu_availability
        # table (checked against this DB) has NO unique constraint on that
        # pair; its PRIMARY KEY is `dish_name` ALONE (menu_availability_pkey),
        # a leftover from before org_id/location_id were added to the table.
        # The existing app code (restaurant_repo.db_set_dish_availability,
        # inventory_repo._sync_dish_availability_conn) both use that exact
        # ON CONFLICT clause and would raise
        # asyncpg.exceptions.InvalidColumnReferenceError on every real call —
        # confirmed by hitting it here first. Worse, since dish_name alone is
        # the PK, it is ALSO a cross-tenant bug: two different orgs cannot
        # both have a dish named e.g. "Pizza" in this table without one
        # clobbering the other's availability row. Reported separately
        # (out of scope for this chunk / this seed script to fix — it needs
        # a real migration to rebuild the PK as (dish_name, org_id) after
        # deduplicating any existing collisions). This script works AROUND
        # it defensively instead of reproducing the crash or the clobber.
        existing_avail = await conn.fetchrow(
            "SELECT org_id FROM menu_availability WHERE dish_name = $1", SOLD_OUT_DISH,
        )
        if existing_avail is None:
            await conn.execute(
                "INSERT INTO menu_availability (dish_name, org_id, available, updated_at) "
                "VALUES ($1, $2, false, NOW())",
                SOLD_OUT_DISH, org_id,
            )
        elif existing_avail["org_id"] == org_id:
            await conn.execute(
                "UPDATE menu_availability SET available = false, updated_at = NOW() WHERE dish_name = $1",
                SOLD_OUT_DISH,
            )
        else:
            print(
                f"  WARNING: menu_availability row for {SOLD_OUT_DISH!r} already belongs to "
                f"org {existing_avail['org_id']} (see the PK bug noted above) — not touching it. "
                "The sold-out demo dish will show as available; pick a more distinctive "
                "SOLD_OUT_DISH name and re-run if you need this scenario."
            )

        await _seed_staff(conn, org_id, CASHIER_NAME, "camila.torres", "caja", CASHIER_PIN)
        await _seed_staff(conn, org_id, COURIER_NAME, "julian.restrepo", "domiciliario", COURIER_PIN)

    return {"org_id": org_id, "centro_id": centro_id, "chapinero_id": chapinero_id}


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: set DATABASE_URL to a local Postgres database (e.g. mesio_tests or mesio_fresh).")
        sys.exit(1)
    _forbid_prod(database_url)

    print("=" * 66)
    print("  Mesio — Delivery/pickup ordering page demo seed (LOCAL ONLY)")
    print("=" * 66)
    print(f"  DB: {database_url}")

    conn = await asyncpg.connect(database_url)
    try:
        result = await seed(conn)
    finally:
        await conn.close()

    print()
    print(f"  org_id = {result['org_id']}")
    print(f"  Sede Centro     location_id = {result['centro_id']}  ({SEDE_CENTRO['lat']}, {SEDE_CENTRO['lon']})")
    print(f"  Sede Chapinero  location_id = {result['chapinero_id']}  ({SEDE_CHAPINERO['lat']}, {SEDE_CHAPINERO['lon']})")
    print()
    print(f"  Sold-out dish (for manual 'Agotado' verification): {SOLD_OUT_DISH!r}")
    print()
    print("  Staff PIN login (name + PIN, against POST /api/staff/pin-login "
          f"with org_id={result['org_id']}):")
    print(f"    Cashier:  name='{CASHIER_NAME}'  pin={CASHIER_PIN}  -> sees Cashier + Domicilios")
    print(f"    Courier:  name='{COURIER_NAME}'  pin={COURIER_PIN}  -> sees Courier")
    print()
    print("  Ordering page:")
    print(f"    /pedir/{ORG_SLUG}")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
