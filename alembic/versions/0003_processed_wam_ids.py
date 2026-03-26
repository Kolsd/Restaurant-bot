"""Add processed_wam_ids table for WhatsApp message deduplication.

The db_is_duplicate_wam() function queries this table on every incoming
WhatsApp webhook to prevent processing the same message twice (Meta retries).

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-26
"""
from alembic import op

revision      = "0003"
down_revision = "0002"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS processed_wam_ids (
            wam_id      TEXT        PRIMARY KEY,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_processed_wam_received "
        "ON processed_wam_ids(received_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processed_wam_ids")
