"""menu_availability: primary key (dish_name) -> (org_id, dish_name).

Every writer upserts with `ON CONFLICT (dish_name, org_id)`
(restaurant_repo.db_set_dish_availability, inventory_repo and orders_repo
`_sync_dish_availability_conn`), but the only unique constraint on the table
was the primary key on `dish_name` alone — the (dish_name, org_id) constraint
that 0037's tenant rework assumed was never recreated. Postgres rejects an
ON CONFLICT target that no constraint matches, so:

  - marking a dish sold out from the admin always raised;
  - selling the last unit of a dish whose linked ingredient hit its minimum
    raised INSIDE the order transaction, rolling back the whole order;
  - and a single-column key means two restaurants cannot both own a dish
    called "Bandeja Paisa" — the second insert collides with a row RLS hides.

The swap is safe on existing data: the old key already guarantees at most one
row per dish_name, hence per (org_id, dish_name); org_id is NOT NULL.

Revision ID: 0085_menu_availability_org_pk
Revises:     0084_orders_customer_email
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0085_menu_availability_org_pk"
down_revision = "0084_orders_customer_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE menu_availability DROP CONSTRAINT IF EXISTS menu_availability_pkey"
    ))
    op.execute(sa.text(
        "ALTER TABLE menu_availability "
        "ADD CONSTRAINT menu_availability_pkey PRIMARY KEY (org_id, dish_name)"
    ))


def downgrade() -> None:
    # Going back to a dish_name-only key is impossible once two tenants share
    # a dish name. Refuse loudly instead of silently deleting one of them.
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM menu_availability
                GROUP BY dish_name HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION '0085 downgrade: several orgs share a dish_name; '
                                'a dish_name-only primary key cannot hold them';
            END IF;
        END $$;
    """))
    op.execute(sa.text(
        "ALTER TABLE menu_availability DROP CONSTRAINT IF EXISTS menu_availability_pkey"
    ))
    op.execute(sa.text(
        "ALTER TABLE menu_availability ADD CONSTRAINT menu_availability_pkey PRIMARY KEY (dish_name)"
    ))
