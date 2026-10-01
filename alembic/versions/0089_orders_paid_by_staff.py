"""orders.paid_by_staff_id — who collected the money on a web order.

Until now `paid = TRUE` was written in exactly one place,
orders_repo.db_confirm_payment, whose only caller is the Wompi webhook —
and Wompi is switched off (docs/claude/payments.md). So no order placed
through the web delivery/pickup channel could ever become paid: not a
transfer with an uploaded receipt, not cash at the door, not the rider's
card reader. Beyond the obvious, that made every delivery sale invisible in
the owner's reports, because stats_repo sums `orders.total WHERE paid=TRUE`.

Payment is now recorded by a person (cashier, admin or the assigned
courier), so the row has to say who — `paid_at` alone cannot answer "who
took this cash". Nullable and ON DELETE SET NULL: orders paid by the Wompi
webhook have no staff behind them, an admin/owner account has no `staff`
row at all, and deleting an employee must never delete sales history.

Revision ID: 0089_orders_paid_by_staff
Revises:     0088_backfill_org_slug
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0089_orders_paid_by_staff"
down_revision = "0088_backfill_org_slug"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_by_staff_id UUID NULL"
    ))
    # Same guarded pattern as 0082's courier_staff_id FK — re-runnable.
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'fk_orders_paid_by_staff_id'
                   AND conrelid = 'orders'::regclass
            ) THEN
                ALTER TABLE orders
                  ADD CONSTRAINT fk_orders_paid_by_staff_id
                  FOREIGN KEY (paid_by_staff_id) REFERENCES staff(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE orders DROP CONSTRAINT IF EXISTS fk_orders_paid_by_staff_id"
    ))
    op.execute(sa.text("ALTER TABLE orders DROP COLUMN IF EXISTS paid_by_staff_id"))
