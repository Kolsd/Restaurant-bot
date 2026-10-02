"""table_orders.ready_at: when the kitchen marked a round "listo".

Mesio HQ shows kitchen time per sede (recibido → listo). table_orders only
had updated_at, which every later change (entregado, factura) overwrites, so
the time could not be measured. Stamped once, the first time the round turns
listo (tables_repo.db_update_table_order_status). Old rows stay NULL.

Revision ID: 0107_table_orders_ready_at
Revises:     0106_nps_location_backfill
Create Date: 2026-10-01
"""
from alembic import op

revision = "0107_table_orders_ready_at"
down_revision = "0106_nps_location_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS ready_at TIMESTAMP")


def downgrade() -> None:
    op.execute("ALTER TABLE table_orders DROP COLUMN IF EXISTS ready_at")
