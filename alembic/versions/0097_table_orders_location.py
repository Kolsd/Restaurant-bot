"""table_orders.location_id — the sede of every table order, finally written.

Found 2026-09-28 in the browser walk-through of a fresh self-serve
restaurant: a diner sent an order from the table QR and the kitchen screen
said "Sin órdenes activas". `db_get_table_orders_for_branch` filters the
sede on `table_orders.location_id`, but `db_save_table_order` never wrote
that column and no trigger fills it on a DB built from migrations, so every
sede-filtered view (kitchen, bar, waiter, an owner who picks a sede) saw no
table orders at all. The insert now writes it; this backfills the rest.

The sede comes from the order's own table (`restaurant_tables.location_id`),
not from `table_orders.branch_id`: 0057 only re-synced `restaurant_tables`,
so old orders may still carry an org id in `branch_id`.

Also: auto-created tables were named "{location_id}-{n}" ("2-1"), so diners
and staff read "Mesa 2-1". New tables are named just "{n}"; existing ones
that still have exactly that generated name are renamed to match.

Revision ID: 0097_table_orders_location
Revises:     0096_restaurants_web_key
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0097_table_orders_location"
down_revision = "0096_restaurants_web_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE table_orders o
           SET location_id = t.location_id
          FROM restaurant_tables t
         WHERE t.id = o.table_id
           AND t.org_id = o.org_id
           AND t.location_id IS NOT NULL
           AND o.location_id IS NULL
    """))
    op.execute(sa.text("""
        UPDATE restaurant_tables
           SET name = number::text
         WHERE number IS NOT NULL
           AND name = branch_id::text || '-' || number::text
    """))


def downgrade() -> None:
    # Data backfill: the old values were wrong (NULL sede, "2-1" names).
    pass
