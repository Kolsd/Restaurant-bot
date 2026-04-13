"""Enhanced reservation and table schema for Apparta features.

Adds capacity, type, zone, and floor-plan position to restaurant_tables.
Extends reservations with status workflow, table assignment, deposits,
no-show tracking, branch filtering, and confirmation-sent flag.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-12
"""
from alembic import op

revision      = "0014"
down_revision = "0013"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # -- restaurant_tables: capacity, type, zone, floor plan position --
    op.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS capacity INTEGER DEFAULT 4")
    op.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS table_type TEXT DEFAULT 'interior'")
    op.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS zone TEXT DEFAULT ''")
    op.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS position_x REAL DEFAULT 0")
    op.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS position_y REAL DEFAULT 0")

    # -- reservations: status workflow, table assignment, deposits, no-show --
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS table_id TEXT DEFAULT NULL")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS confirmation_sent BOOLEAN DEFAULT FALSE")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ DEFAULT NULL")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ DEFAULT NULL")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS cancellation_reason TEXT DEFAULT ''")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS deposit_amount NUMERIC(12,2) DEFAULT 0")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS deposit_paid BOOLEAN DEFAULT FALSE")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS deposit_transaction_id TEXT DEFAULT ''")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS no_show BOOLEAN DEFAULT FALSE")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS branch_id INTEGER DEFAULT NULL")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'whatsapp'")
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS restaurant_id INTEGER DEFAULT NULL")

    # -- indexes --
    op.execute("CREATE INDEX IF NOT EXISTS idx_reservations_date_status ON reservations(date, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reservations_table_date ON reservations(table_id, date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reservations_branch ON reservations(branch_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_restaurant_tables_type ON restaurant_tables(table_type)")

    # -- backfill: mark existing reservations as confirmed --
    op.execute("UPDATE reservations SET status = 'confirmed' WHERE status IS NULL OR status = 'pending'")


def downgrade() -> None:
    pass
