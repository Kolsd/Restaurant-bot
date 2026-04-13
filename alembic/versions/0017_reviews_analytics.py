"""Reviews (extend NPS) + occupancy_snapshots for analytics

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-12
"""
from alembic import op

revision      = "0017"
down_revision = "0016"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # Extend NPS responses into a public reviews system
    op.execute("ALTER TABLE nps_responses ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT FALSE")
    op.execute("ALTER TABLE nps_responses ADD COLUMN IF NOT EXISTS owner_reply TEXT DEFAULT ''")
    op.execute("ALTER TABLE nps_responses ADD COLUMN IF NOT EXISTS owner_reply_at TIMESTAMPTZ DEFAULT NULL")
    op.execute("ALTER TABLE nps_responses ADD COLUMN IF NOT EXISTS customer_name TEXT DEFAULT ''")

    # Occupancy snapshots for analytics (turn time, utilization)
    op.execute("""
        CREATE TABLE IF NOT EXISTS occupancy_snapshots (
            id BIGSERIAL PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            branch_id INTEGER DEFAULT NULL,
            snapshot_at TIMESTAMPTZ DEFAULT NOW(),
            total_tables INTEGER NOT NULL DEFAULT 0,
            occupied_tables INTEGER NOT NULL DEFAULT 0,
            total_capacity INTEGER NOT NULL DEFAULT 0,
            seated_guests INTEGER NOT NULL DEFAULT 0
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_occupancy_snapshots_time "
        "ON occupancy_snapshots(restaurant_id, snapshot_at)"
    )


def downgrade() -> None:
    pass
