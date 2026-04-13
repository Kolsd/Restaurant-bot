"""Dynamic time-slot discounts (yield management)

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-12
"""
from alembic import op

revision      = "0015"
down_revision = "0014"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS time_slot_discounts (
            id SERIAL PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            branch_id INTEGER DEFAULT NULL,
            day_of_week INTEGER NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 5 AND 50),
            label TEXT DEFAULT '',
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (restaurant_id, branch_id, day_of_week, start_time)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_time_slot_discounts_active "
        "ON time_slot_discounts(restaurant_id, active)"
    )


def downgrade() -> None:
    pass
