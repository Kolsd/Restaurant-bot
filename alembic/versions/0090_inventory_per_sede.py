"""inventory.location_id backfill + index — stock belongs to ONE sede.

PM decision 2026-09-20: "el inventario es uno por sede, se puede hacer
intercambios de inventario por sede". The column has existed since the
org/location wave but nothing ever wrote it: every row was created org-wide,
so a two-sede restaurant had one shared stock number that neither kitchen
could trust, and an order at sede B decremented whatever row the recipe
happened to point at.

Backfill sends existing rows to the org's FIRST location (lowest id — the
"Principal" that CRM conversion creates), because that is where a
single-sede restaurant's stock actually is, and a multi-sede one has to
re-split it by hand anyway; guessing a different sede per row would be
inventing data.

The column stays NULLABLE on purpose. An org with no `locations` row at all
(legacy data) would otherwise fail the backfill and block the migration, and
the read paths treat NULL as "visible from every sede" so nothing silently
disappears from a stock list. New rows always carry one — the API refuses a
create without a sede.

Revision ID: 0090_inventory_per_sede
Revises:     0089_orders_paid_by_staff
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0090_inventory_per_sede"
down_revision = "0089_orders_paid_by_staff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE inventory i
           SET location_id = l.id
          FROM (
                SELECT DISTINCT ON (org_id) org_id, id
                  FROM locations
                 ORDER BY org_id, id
               ) l
         WHERE i.location_id IS NULL
           AND l.org_id = i.org_id
    """))

    # Every read is now "this org, this sede" — the org_id-only index left
    # that as a filter on top of a scan of every sede's stock.
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_inventory_org_location "
        "ON inventory (org_id, location_id)"
    ))

    # Transfers match "the same product at another sede" by name within the
    # org (app/repositories/inventory_repo.db_transfer_inventory), so that
    # lookup needs to be indexed too. Not UNIQUE: existing data may already
    # hold duplicate names in one sede, and failing the migration over that
    # would be worse than a slightly slower match.
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_inventory_org_name "
        "ON inventory (org_id, lower(name))"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_inventory_org_name"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_inventory_org_location"))
    # The backfilled location_id is left in place: clearing it would throw
    # away the sede assignment an owner has since corrected by hand.
