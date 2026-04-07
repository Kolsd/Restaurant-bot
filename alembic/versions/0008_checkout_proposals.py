"""Checkout proposals — bot-driven payment flow for table checks.

Adds proposal columns to table_checks to support the WhatsApp checkout
conversation flow: split, tip capture, payment methods, proof image.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-06
"""
from alembic import op

revision      = "0009"
down_revision = "0008"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE table_checks
      ADD COLUMN IF NOT EXISTS proposed_payments      JSONB,
      ADD COLUMN IF NOT EXISTS proposed_tip           NUMERIC(10,2),
      ADD COLUMN IF NOT EXISTS proposal_source        TEXT,
      ADD COLUMN IF NOT EXISTS proposal_status        TEXT,
      ADD COLUMN IF NOT EXISTS proof_media_url        TEXT,
      ADD COLUMN IF NOT EXISTS proposal_created_at    TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS proposal_customer_phone TEXT
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_table_checks_proposal_status
      ON table_checks (proposal_status)
      WHERE proposal_status IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_table_checks_proposal_status")
    op.execute("""
    ALTER TABLE table_checks
      DROP COLUMN IF EXISTS proposed_payments,
      DROP COLUMN IF EXISTS proposed_tip,
      DROP COLUMN IF EXISTS proposal_source,
      DROP COLUMN IF EXISTS proposal_status,
      DROP COLUMN IF EXISTS proof_media_url,
      DROP COLUMN IF EXISTS proposal_created_at,
      DROP COLUMN IF EXISTS proposal_customer_phone
    """)
