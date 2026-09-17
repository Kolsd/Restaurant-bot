"""
scripts/dev/seed_staff_app_demo.py — Local-only seed for manually verifying
the unified Staff App (/staff) in a browser.

Creates, idempotently:
  - One organization + one location ("sede principal")
  - One admin user (owner) — logs in via /login with email+password
  - One staff member with ONLY the "mesero" role (sees Waiter + Mi turno)
  - One staff member with "caja" + "mesero" roles (sees Cashier + Waiter + Mi turno)
  - Two restaurant tables
  - One open table_order on the first table, so Cashier/Waiter/Kitchen show data

NEVER run this against a production database — it hashes and prints real
looking (but dummy, local-only) credentials to stdout. Intended DB: the
local unit-test database (mesio_tests), same one the verification uvicorn
instance in the Staff App task used (see project CLAUDE.md, "Comandos").

Usage:
    DATABASE_URL=postgresql://postgres:mesio_local_dev@localhost:5432/mesio_tests \\
    .venv/Scripts/python.exe scripts/dev/seed_staff_app_demo.py

Idempotent: safe to run multiple times (upserts by unique whatsapp_number /
username), and only ever touches the target database given via DATABASE_URL.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import asyncpg
from passlib.hash import bcrypt as pin_bcrypt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.services.password_hash import hash_password  # noqa: E402

ORG_NAME = "Mesio Staff App Demo"
ORG_SLUG = "staff-app-demo"
BOT_NUMBER = "+570STAFFDEMO"

ADMIN_EMAIL = "admin@staffdemo.mesio.test"
ADMIN_PASSWORD = "StaffDemo123!"

WAITER_NAME = "Valentina Ríos"
WAITER_PIN = "1111"

CASHIER_WAITER_NAME = "Andrés Gómez"
CASHIER_WAITER_PIN = "2222"

MENU = {
    "Platos": [
        {"name": "Bandeja Paisa", "description": "Plato típico", "price": 28000, "active": True},
        {"name": "Sancocho", "description": "Sopa tradicional", "price": 22000, "active": True},
    ],
    "Bebidas": [
        {"name": "Limonada", "description": "Limonada natural", "price": 6000, "active": True},
    ],
}

FEATURES = {
    "bot_active": True,
    "domicilio_active": True,
    "recoger_active": True,
    "staff_tips": True,
    "currency": "COP",
    "locale": "es-CO",
    "timezone": "America/Bogota",
    "payment_methods": ["Efectivo", "Nequi"],
}


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


async def seed(conn: asyncpg.Connection) -> dict:
    async with conn.transaction():
        org_row = await conn.fetchrow(
            """
            INSERT INTO organizations (name, slug, whatsapp_number, menu, features)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            ON CONFLICT (whatsapp_number) WHERE whatsapp_number IS NOT NULL
              DO UPDATE SET name = EXCLUDED.name, menu = EXCLUDED.menu, features = EXCLUDED.features
            RETURNING id
            """,
            ORG_NAME, ORG_SLUG, BOT_NUMBER, json.dumps(MENU), json.dumps(FEATURES),
        )
        org_id = org_row["id"]

        loc_row = await conn.fetchrow(
            "SELECT id FROM locations WHERE org_id = $1 AND whatsapp_number IS NULL ORDER BY id ASC LIMIT 1",
            org_id,
        )
        if loc_row:
            location_id = loc_row["id"]
        else:
            loc_row = await conn.fetchrow(
                """
                INSERT INTO locations (org_id, name, code, address, active, timezone)
                VALUES ($1, $2, 'principal', $3, true, 'America/Bogota')
                RETURNING id
                """,
                org_id, ORG_NAME, "Calle 10 #20-30, Bogotá (demo local)",
            )
            location_id = loc_row["id"]

        # ── Admin user (real, usable password — /login with email+password) ──
        admin_hash = hash_password(ADMIN_PASSWORD)
        await conn.execute(
            """
            INSERT INTO users (username, password_hash, restaurant_name, role, branch_id, org_id, location_id)
            VALUES ($1, $2, $3, 'owner', $4::integer, $5::integer, $6)
            ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                                                  org_id = EXCLUDED.org_id, location_id = EXCLUDED.location_id
            """,
            ADMIN_EMAIL, admin_hash, ORG_NAME, org_id, org_id, location_id,
        )

        # ── Staff: waiter-only ────────────────────────────────────────────
        await conn.execute("SET LOCAL ROLE mesio_app")
        await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))

        waiter_pin_hash = pin_bcrypt.hash(WAITER_PIN)
        existing_waiter = await conn.fetchrow(
            "SELECT id FROM staff WHERE org_id = $1 AND name = $2", org_id, WAITER_NAME
        )
        if existing_waiter:
            waiter_id = existing_waiter["id"]
            await conn.execute(
                "UPDATE staff SET pin = $2, role = 'mesero', roles = '[\"mesero\"]'::jsonb, active = true WHERE id = $1",
                waiter_id, waiter_pin_hash,
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO staff (org_id, name, username, role, pin, phone, roles, document_number)
                   VALUES ($1, $2, $3, 'mesero', $4, '', '["mesero"]'::jsonb, '')
                   RETURNING id""",
                org_id, WAITER_NAME, "valentina.rios", waiter_pin_hash,
            )
            waiter_id = row["id"]

        cw_pin_hash = pin_bcrypt.hash(CASHIER_WAITER_PIN)
        existing_cw = await conn.fetchrow(
            "SELECT id FROM staff WHERE org_id = $1 AND name = $2", org_id, CASHIER_WAITER_NAME
        )
        if existing_cw:
            cw_id = existing_cw["id"]
            await conn.execute(
                "UPDATE staff SET pin = $2, role = 'caja', roles = '[\"caja\", \"mesero\"]'::jsonb, active = true WHERE id = $1",
                cw_id, cw_pin_hash,
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO staff (org_id, name, username, role, pin, phone, roles, document_number)
                   VALUES ($1, $2, $3, 'caja', $4, '', '["caja", "mesero"]'::jsonb, '')
                   RETURNING id""",
                org_id, CASHIER_WAITER_NAME, "andres.gomez", cw_pin_hash,
            )
            cw_id = row["id"]

        # ── Two tables ────────────────────────────────────────────────────
        table_ids = []
        for i, (number, name) in enumerate([(1, "Mesa 1"), (2, "Mesa 2")]):
            existing_table = await conn.fetchrow(
                "SELECT id FROM restaurant_tables WHERE org_id = $1 AND name = $2", org_id, name
            )
            if existing_table:
                table_id = existing_table["id"]
            else:
                table_id = str(uuid.uuid4())
                await conn.execute(
                    """INSERT INTO restaurant_tables
                         (id, org_id, branch_id, location_id, number, name, capacity, active)
                       VALUES ($1, $2, $3::integer, $3::bigint, $4, $5, 4, true)""",
                    table_id, org_id, location_id, number, name,
                )
            table_ids.append(table_id)

        # ── One open table session + order on Mesa 1 ──────────────────────
        # The Waiter/Cashier "tables-status" enrichment query
        # (app/repositories/tables_repo.py::db_get_tables_status_enrichment)
        # reads table_orders/table_sessions filtered by `branch_id` (a
        # separate column from `location_id`, kept equal to it post-0057 —
        # see the "branch_id-guard-allow" comment in tables_repo.py) and
        # needs a `table_sessions` row with status='active' for the tile to
        # show as occupied at all (`session_active`/`bot_active`). Both are
        # required or the table renders as "Libre" / $0 even with an order.
        existing_session = await conn.fetchrow(
            "SELECT id FROM table_sessions WHERE org_id = $1 AND table_id = $2 AND status = 'active' LIMIT 1",
            org_id, table_ids[0],
        )
        if not existing_session:
            await conn.execute(
                """INSERT INTO table_sessions
                     (table_id, table_name, phone, bot_number, status, has_order,
                      org_id, location_id)
                   VALUES ($1, 'Mesa 1', $2, $3, 'active', true, $4, $5)""",
                table_ids[0], "573000000000", BOT_NUMBER, org_id, location_id,
            )

        existing_order = await conn.fetchrow(
            "SELECT id FROM table_orders WHERE org_id = $1 AND table_id = $2 AND status != 'entregado' LIMIT 1",
            org_id, table_ids[0],
        )
        if existing_order:
            # Re-run safety net: an order created before this script set
            # branch_id would otherwise stay invisible to tables-status forever.
            await conn.execute(
                "UPDATE table_orders SET branch_id = $2::integer WHERE id = $1",
                existing_order["id"], location_id,
            )
        else:
            order_id = str(uuid.uuid4())
            items = json.dumps([{"name": "Bandeja Paisa", "qty": 2, "price": 28000}])
            await conn.execute(
                """INSERT INTO table_orders
                     (id, org_id, location_id, branch_id, table_id, table_name, phone,
                      status, items, total, bot_number, channel)
                   VALUES ($1, $2, $3, $3::integer, $4, 'Mesa 1', $5, 'recibido',
                           $6::jsonb, $7, $8, 'manual')""",
                order_id, org_id, location_id, table_ids[0], "573000000000",
                items, 56000, BOT_NUMBER,
            )

    return {"org_id": org_id, "location_id": location_id}


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: set DATABASE_URL to a local Postgres database (e.g. mesio_tests).")
        sys.exit(1)
    _forbid_prod(database_url)

    print("=" * 66)
    print("  Mesio — Staff App demo seed (LOCAL ONLY)")
    print("=" * 66)
    print(f"  DB: {database_url}")

    conn = await asyncpg.connect(database_url)
    try:
        result = await seed(conn)
    finally:
        await conn.close()

    print()
    print(f"  org_id = {result['org_id']}   location_id = {result['location_id']}")
    print()
    print("  Login at /login with:")
    print(f"    Admin (sees every section):  {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print()
    print("  Staff PIN login (name + PIN, restaurant_id is asked by the login form"
          f" or use org_id={result['org_id']} directly against POST /api/staff/pin-login):")
    print(f"    Waiter only:        name='{WAITER_NAME}'         pin={WAITER_PIN}   -> sees Waiter + Mi turno")
    print(f"    Cashier + Waiter:   name='{CASHIER_WAITER_NAME}'  pin={CASHIER_WAITER_PIN}   -> sees Cashier + Waiter + Mi turno")
    print()
    print("  2 tables seeded (Mesa 1, Mesa 2); Mesa 1 has one open order (2x Bandeja Paisa).")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
