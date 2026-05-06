"""Demo Mesio org — seed a shared demo restaurant for the /demo live chat widget.

Seeds one organization (Demo Mesio) with a realistic Colombian menu and a comp
subscription so billing cap enforcement never blocks demo sessions.

Idempotent: every INSERT uses ON CONFLICT DO NOTHING.
Cross-RLS: INSERTs run inside SET LOCAL ROLE mesio_superadmin blocks so they
pass WITH CHECK policies on RLS-protected tables.

Revision ID: 0071_demo_seed
Revises: 0069_password_reset_tokens
"""

import json
import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0071_demo_seed"
down_revision = "0069_password_reset_tokens"
branch_labels = None
depends_on = None

# ── Demo constants ────────────────────────────────────────────────────────────

DEMO_BOT_NUMBER = "demo:0"   # synthetic — never matches real WhatsApp numbers
DEMO_ORG_NAME = "Demo Mesio"
DEMO_SLUG = "demo-mesio"
DEMO_ADDRESS = "Calle 93 #11-27, Bogotá (Demo)"

DEMO_MENU = {
    "categories": [
        {
            "name": "Entradas",
            "items": [
                {
                    "name": "Empanadas de pipián",
                    "description": "3 empanadas doradas rellenas de pipián con ají de maní y hogao",
                    "price": 9000,
                    "sku": "DEMO-ENT-001",
                    "available": True,
                    "tags": ["popular"],
                    "badges": ["chef_pick"],
                },
                {
                    "name": "Patacones con hogao",
                    "description": "Patacones fritos con hogao casero y queso costeño rallado",
                    "price": 11000,
                    "sku": "DEMO-ENT-002",
                    "available": True,
                    "tags": [],
                    "badges": [],
                },
                {
                    "name": "Ceviche de camarón",
                    "description": "Camarón fresco marinado en limón, cilantro, tomate y aguacate",
                    "price": 18000,
                    "sku": "DEMO-ENT-003",
                    "available": True,
                    "tags": ["popular"],
                    "badges": [],
                },
            ],
        },
        {
            "name": "Platos Fuertes",
            "items": [
                {
                    "name": "Bandeja Paisa",
                    "description": "Frijoles, chicharrón, carne molida, chorizo, morcilla, arepa, arroz y huevo frito",
                    "price": 28000,
                    "sku": "DEMO-PF-001",
                    "available": True,
                    "tags": ["popular"],
                    "badges": ["chef_pick"],
                },
                {
                    "name": "Ajiaco Santafereño",
                    "description": "Sopa bogotana con tres tipos de papa, pollo desmenuzado, guasca y crema de leche",
                    "price": 22000,
                    "sku": "DEMO-PF-002",
                    "available": True,
                    "tags": [],
                    "badges": [],
                },
                {
                    "name": "Cazuela de Mariscos",
                    "description": "Camarón, calamar y mejillones en salsa de coco con patacón",
                    "price": 32000,
                    "sku": "DEMO-PF-003",
                    "available": True,
                    "tags": [],
                    "badges": ["new"],
                },
                {
                    "name": "Lomo de res en salsa de champiñones",
                    "description": "Lomo fino a la plancha con salsa cremosa de champiñones silvestres y papa criolla",
                    "price": 35000,
                    "sku": "DEMO-PF-004",
                    "available": True,
                    "tags": [],
                    "badges": [],
                },
            ],
        },
        {
            "name": "Postres y Bebidas",
            "items": [
                {
                    "name": "Tres leches",
                    "description": "Bizcocho bañado en tres tipos de leche con crema chantilly y fresas",
                    "price": 12000,
                    "sku": "DEMO-POS-001",
                    "available": True,
                    "tags": ["popular"],
                    "badges": [],
                },
                {
                    "name": "Oblea con arequipe",
                    "description": "Oblea crujiente con arequipe, mermelada de mora y coco rallado",
                    "price": 8000,
                    "sku": "DEMO-POS-002",
                    "available": True,
                    "tags": [],
                    "badges": [],
                },
                {
                    "name": "Limonada de panela",
                    "description": "Limonada fría con panela orgánica, hierbabuena y limón",
                    "price": 7000,
                    "sku": "DEMO-BEB-001",
                    "available": True,
                    "tags": [],
                    "badges": [],
                },
                {
                    "name": "Cerveza Club Colombia",
                    "description": "Botella 330ml bien fría",
                    "price": 8000,
                    "sku": "DEMO-BEB-002",
                    "available": True,
                    "tags": [],
                    "badges": [],
                },
                {
                    "name": "Agua mineral",
                    "description": "Botella 600ml",
                    "price": 4000,
                    "sku": "DEMO-BEB-003",
                    "available": True,
                    "tags": [],
                    "badges": [],
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
        "Nequi": "Enviar al 300-000-0000 (Demo)",
        "Daviplata": "Enviar al 300-000-0000 (Demo)",
        "Transferencia Bancaria": "Banco de Bogotá - Cuenta de Ahorros 000000000 (Demo)",
    },
    "bot_active": True,
    "domicilio_active": True,
    "recoger_active": True,
    "upsell_active": True,
    "currency": "COP",
    "locale": "es-CO",
    "timezone": "America/Bogota",
    "admin_phone": "3000000000",
    "delivery_radius_km": 5,
    "module_reservations": True,
    "subscription_plan": "pro",
    "comp_until": "2099-12-31T00:00:00+00:00",
}


def upgrade() -> None:
    conn = op.get_bind()

    # ── Switch to mesio_superadmin to bypass RLS ──────────────────────────────
    # organizations and locations have RLS + FORCE, so we need the bypass role
    # for the INSERT statements.
    conn.execute(sa.text("SET LOCAL ROLE mesio_superadmin"))

    logger.info("0071 upgrade: seeding Demo Mesio org")

    # ── 1. Organization ───────────────────────────────────────────────────────
    org_id = conn.execute(
        sa.text("""
            INSERT INTO organizations (name, whatsapp_number, menu, features, slug)
            VALUES (:name, :wanum, :menu::jsonb, :features::jsonb, :slug)
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
        """),
        {
            "name": DEMO_ORG_NAME,
            "wanum": DEMO_BOT_NUMBER,
            "menu": json.dumps(DEMO_MENU),
            "features": json.dumps(DEMO_FEATURES),
            "slug": DEMO_SLUG,
        },
    ).scalar()

    if org_id is None:
        # Already exists — look up the id for the location step
        org_id = conn.execute(
            sa.text("SELECT id FROM organizations WHERE slug = :slug"),
            {"slug": DEMO_SLUG},
        ).scalar()
        logger.info("0071 upgrade: Demo org already exists, org_id=%s", org_id)
    else:
        logger.info("0071 upgrade: Demo org created, org_id=%s", org_id)

    # ── 2. Location ───────────────────────────────────────────────────────────
    loc_exists = conn.execute(
        sa.text("SELECT 1 FROM locations WHERE org_id = :oid LIMIT 1"),
        {"oid": org_id},
    ).scalar()

    if not loc_exists:
        loc_id = conn.execute(
            sa.text("""
                INSERT INTO locations (org_id, name, code, address, active, timezone)
                VALUES (:oid, :name, 'principal', :addr, true, 'America/Bogota')
                RETURNING id
            """),
            {"oid": org_id, "name": DEMO_ORG_NAME, "addr": DEMO_ADDRESS},
        ).scalar()
        logger.info("0071 upgrade: Demo location created, loc_id=%s", loc_id)
    else:
        logger.info("0071 upgrade: Demo location already exists")

    logger.info("0071 upgrade: done")


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("SET LOCAL ROLE mesio_superadmin"))

    # Remove Demo org and its location (CASCADE takes the location if FK exists)
    conn.execute(
        sa.text("DELETE FROM locations WHERE org_id = (SELECT id FROM organizations WHERE slug = :slug)"),
        {"slug": DEMO_SLUG},
    )
    conn.execute(
        sa.text("DELETE FROM organizations WHERE slug = :slug"),
        {"slug": DEMO_SLUG},
    )
    logger.info("0071 downgrade: Demo Mesio org removed")
