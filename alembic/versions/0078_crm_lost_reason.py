"""Add lost_reason and lost_at columns to prospects table.

Captures why a prospect was lost — enables conversion funnel analysis.

Revision ID: 0078_crm_lost_reason
Revises:     0077_hq_audit_log
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0078_crm_lost_reason"
down_revision = "0077_hq_audit_log"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lost_reason TEXT"
    ))
    op.execute(sa.text(
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lost_at TIMESTAMPTZ"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_prospects_lost_reason "
        "ON prospects(lost_reason) WHERE lost_reason IS NOT NULL"
    ))


def downgrade():
    op.execute(sa.text("DROP INDEX IF EXISTS ix_prospects_lost_reason"))
    op.execute(sa.text("ALTER TABLE prospects DROP COLUMN IF EXISTS lost_reason"))
    op.execute(sa.text("ALTER TABLE prospects DROP COLUMN IF EXISTS lost_at"))
