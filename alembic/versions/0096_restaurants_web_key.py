"""restaurants.whatsapp_number — a web key for orgs that have no WhatsApp.

Found 2026-09-25: an org created by self-serve signup has no WhatsApp
number, so the view's `COALESCE(l.whatsapp_number, o.whatsapp_number)` was
NULL and every web flow that still keys on it refused to start — the table
QR (`POST /api/diner/session`) and `/pedir` both answered "Restaurante no
configurado". No restaurant that signed up by itself could take an order.

The column is still the tenant key the bot runtime calls `bot_number`
(sessions, conversations, carts, NPS). Rather than rewrite that in one go,
the view now falls back to `'web' || org_id` — one key per ORGANIZATION,
exactly like a WhatsApp org whose sedes all share the org's number, so every
org-wide aggregation keeps working and the sede is still told apart by
`location_id` as it already is. Orgs with a real number see no change.

A real phone number never starts with letters, so the prefix cannot collide
with one. `app/services/channel_key.py` is the Python mirror; anything that
builds a wa.me link must skip a web key.

Revision ID: 0096_restaurants_web_key
Revises:     0095_prospects_full_schema
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0096_restaurants_web_key"
down_revision = "0095_prospects_full_schema"
branch_labels = None
depends_on = None


def _view(number_expr: str) -> str:
    return f"""
    CREATE OR REPLACE VIEW restaurants AS
    SELECT l.id,
        COALESCE(o.name, l.name) AS name,
        l.name AS location_name,
        {number_expr} AS whatsapp_number,
        l.address,
        l.latitude,
        l.longitude,
        o.menu,
        o.features,
        COALESCE(l.wa_phone_id, o.wa_phone_id) AS wa_phone_id,
        COALESCE(l.wa_access_token, o.wa_access_token) AS wa_access_token,
        o.slug,
        o.billing_config,
        o.subscription_status,
        o.subscription_plan,
        l.created_at,
        l.updated_at,
        CASE
            WHEN (SELECT count(*) FROM locations l2 WHERE l2.org_id = o.id) > 1
                 AND l.name IS NOT NULL
                 AND btrim(l.name) <> ''
                 AND lower(btrim(l.name)) IS DISTINCT FROM lower(btrim(COALESCE(o.name, '')))
            THEN COALESCE(o.name, l.name) || ' · ' || l.name
            ELSE COALESCE(o.name, l.name)
        END AS display_name
    FROM locations l
    JOIN organizations o ON o.id = l.org_id
    """


def upgrade() -> None:
    op.execute(sa.text(_view(
        "COALESCE(l.whatsapp_number, o.whatsapp_number, 'web' || o.id::text)"
    )))


def downgrade() -> None:
    op.execute(sa.text(_view("COALESCE(l.whatsapp_number, o.whatsapp_number)")))
