"""restaurants.display_name — say WHICH sede, when there is more than one.

PM 2026-09-20: each sede is its own restaurant. The view's `name` is
`COALESCE(o.name, l.name)`, i.e. the organization's — so every sede of a
chain introduced itself with the brand and the diner had no way to tell
which one they were in, on the chat greeting, the order status page, the
emails or the kitchen ticket.

`display_name` is "Marca · Sede" for an org with more than one location and
plain "Marca" for an org with one. Single-sede restaurants — almost all of
them today — see no change at all, which is the point: the sede suffix is
noise until it disambiguates something.

`name` is deliberately LEFT ALONE. It is read by dozens of call sites,
several of which feed tenant resolution and templates, and flipping its
meaning underneath them would be a silent rename of the whole product. New
column, opt-in at each place that shows a name to a human.

CREATE OR REPLACE keeps the view's identity (no dependent views, rules or
triggers exist on it — checked), and the new column is appended last so
every existing column keeps its name, type and position.

Revision ID: 0092_restaurants_display_name
Revises:     0091_menu_availability_sede
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0092_restaurants_display_name"
down_revision = "0091_menu_availability_sede"
branch_labels = None
depends_on = None


_VIEW_COLUMNS = """
    SELECT l.id,
        COALESCE(o.name, l.name) AS name,
        l.name AS location_name,
        COALESCE(l.whatsapp_number, o.whatsapp_number) AS whatsapp_number,
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
        l.updated_at
"""

_DISPLAY_NAME = """,
        CASE
            WHEN (SELECT count(*) FROM locations l2 WHERE l2.org_id = o.id) > 1
                 AND l.name IS NOT NULL
                 AND btrim(l.name) <> ''
                 -- A sede named after the org ("Mesio Demo" inside "Mesio
                 -- Demo") would render the brand twice. Seeds and older
                 -- onboarding do exactly that.
                 AND lower(btrim(l.name)) IS DISTINCT FROM lower(btrim(COALESCE(o.name, '')))
            THEN COALESCE(o.name, l.name) || ' · ' || l.name
            ELSE COALESCE(o.name, l.name)
        END AS display_name
"""

_FROM = """
    FROM locations l
    JOIN organizations o ON o.id = l.org_id
"""


def upgrade() -> None:
    op.execute(sa.text(
        "CREATE OR REPLACE VIEW restaurants AS" + _VIEW_COLUMNS + _DISPLAY_NAME + _FROM
    ))


def downgrade() -> None:
    # Dropping a column from a view needs a full DROP; nothing depends on it.
    op.execute(sa.text("DROP VIEW IF EXISTS restaurants"))
    op.execute(sa.text("CREATE VIEW restaurants AS" + _VIEW_COLUMNS + _FROM))
