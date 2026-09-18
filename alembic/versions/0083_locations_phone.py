"""Add locations.phone — customer-facing contact number for the delivery/
pickup web entry point (docs/claude/delivery-web.md, chunk 2).

Why a NEW column instead of reusing something that exists: the chunk-2 spec
(`GET /api/diner/org/{slug}`) needs to show each sede's phone number so a
customer can call the restaurant, and the customer status page (a later
chunk) needs the same number for its "llamar al restaurante" button. The
only phone-shaped value already on `locations` is `whatsapp_number`, but
that column is explicitly NOT allowed to leak through the public entry
point (docs/claude/delivery-web.md: "No WhatsApp tokens" on the public org
endpoint) — it is a bot-routing identity key, not necessarily the number a
customer should dial, and conflating the two would silently reintroduce the
exact kind of "one column, two meanings" ambiguity this codebase has already
been burned by (see memory/ambiguous-restaurant-lookup-p0.md). So a genuine,
separate, nullable `phone` column is added instead of overloading
`whatsapp_number`.

Nullable, no backfill: existing locations do not have a known customer
contact number today. Reported loudly per CHUNK-2 instructions: this is a
NEW migration, `0082_delivery_web` (chunk 1) is left untouched.

`locations` has NO RLS policy (see docs/claude/rls-multitenant.md — it is
not in `_RLS_TABLES`), so no RLS changes are needed here.

Revision ID: 0083_locations_phone
Revises:     0082_delivery_web
Create Date: 2026-09-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0083_locations_phone"
down_revision = "0082_delivery_web"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE locations ADD COLUMN IF NOT EXISTS phone TEXT"))
    logger.info("0083: locations.phone added")


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE locations DROP COLUMN IF EXISTS phone"))
