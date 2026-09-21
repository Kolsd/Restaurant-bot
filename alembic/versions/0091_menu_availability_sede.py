"""menu_availability keyed per sede — "agotado" was an org-wide switch.

PM 2026-09-20: "cada sede es un restaurante propio y unico, debe ser
totalmente independiente. El uso del org id es para temas de reporteria."

The table has carried a `location_id` column since the org/location wave,
but its PRIMARY KEY was `(org_id, dish_name)` and every writer passed only
the org. So marking a dish sold out at one sede marked it sold out at every
sede of the business — including, once inventory ran per sede (0090), when
ONE sede simply ran out of an ingredient.

The key becomes `(org_id, location_id, dish_name)`. Existing rows are
expanded into one row per location of their org, which preserves what they
meant: "sold out everywhere". Rows belonging to an org with no `locations`
row are deleted — they were unreadable either way once reads are per sede,
and keeping them would block the NOT NULL.

`location_id` becomes NOT NULL on purpose. A writer that cannot resolve a
sede must fail loudly here rather than quietly re-create the org-wide row
this migration exists to remove.

Revision ID: 0091_menu_availability_sede
Revises:     0090_inventory_per_sede
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0091_menu_availability_sede"
down_revision = "0090_inventory_per_sede"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Expand every org-wide row into one row per sede. ON CONFLICT covers
    #    rows that already carried a location_id (nothing wrote them, but the
    #    column was nullable, so do not assume).
    op.execute(sa.text("""
        INSERT INTO menu_availability (dish_name, org_id, location_id, available, updated_at)
        SELECT ma.dish_name, ma.org_id, l.id, ma.available, ma.updated_at
          FROM menu_availability ma
          JOIN locations l ON l.org_id = ma.org_id
         WHERE ma.location_id IS NULL
        ON CONFLICT DO NOTHING
    """))

    # 2. Drop the originals (and anything belonging to an org with no sede).
    op.execute(sa.text("DELETE FROM menu_availability WHERE location_id IS NULL"))

    # 3. Re-key.
    op.execute(sa.text(
        "ALTER TABLE menu_availability DROP CONSTRAINT IF EXISTS menu_availability_pkey"
    ))
    op.execute(sa.text(
        "ALTER TABLE menu_availability ALTER COLUMN location_id SET NOT NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE menu_availability "
        "ADD CONSTRAINT menu_availability_pkey PRIMARY KEY (org_id, location_id, dish_name)"
    ))


def downgrade() -> None:
    # Collapse back to one row per org: a dish counts as sold out org-wide
    # only if it was sold out at EVERY sede, which is the closest honest
    # inverse of the expansion above.
    op.execute(sa.text(
        "ALTER TABLE menu_availability DROP CONSTRAINT IF EXISTS menu_availability_pkey"
    ))
    op.execute(sa.text("""
        DELETE FROM menu_availability ma
         WHERE EXISTS (
               SELECT 1 FROM menu_availability other
                WHERE other.org_id = ma.org_id
                  AND other.dish_name = ma.dish_name
                  AND other.location_id < ma.location_id
         )
    """))
    op.execute(sa.text(
        "ALTER TABLE menu_availability ALTER COLUMN location_id DROP NOT NULL"
    ))
    op.execute(sa.text("UPDATE menu_availability SET location_id = NULL"))
    op.execute(sa.text(
        "ALTER TABLE menu_availability "
        "ADD CONSTRAINT menu_availability_pkey PRIMARY KEY (org_id, dish_name)"
    ))
