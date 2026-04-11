"""Add branch_id to table_orders for multi-branch kitchen filtering.

This column was missing from the initial schema but has been referenced in
db_save_table_order and get_table_orders since multi-branch support was added.
Running alembic upgrade head on a fresh DB was failing silently: the INSERT
into table_orders raised 'column branch_id does not exist', the exception was
caught by execute_action's outer try/except, and no order was ever saved to the
kitchen queue.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-10
"""
from alembic import op

revision      = "0013"
down_revision = "0012"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE table_orders
            ADD COLUMN IF NOT EXISTS branch_id INTEGER
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_table_orders_branch
            ON table_orders (branch_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_table_orders_branch")
    op.execute("ALTER TABLE table_orders DROP COLUMN IF EXISTS branch_id")
