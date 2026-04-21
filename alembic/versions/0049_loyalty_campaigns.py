"""Loyalty campaigns: automation table for WhatsApp-based marketing campaigns.

Adds one table:
  - loyalty_campaigns: stores campaign definitions with trigger type, message
    template, status lifecycle (draft → active ↔ paused), and performance
    counters (sent_count, converted_count, revenue_numeric).

RLS: org_isolation policy (same pattern as 0042_staff_comms, 0043_shift_swap).
GUC used: app.org_id (Wave-2 canonical tenant key).

Revision ID: 0049_loyalty_camps
Revises: 0048_performance_indexes
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0049_loyalty_camps"
down_revision = "0048_performance_indexes"
branch_labels = None
depends_on = None

_ORG_ISOLATION_EXPR = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)

# Allowed trigger types — informational, the CHECK is on status only.
# trigger_type is a free-form text column to stay forward-compatible.
# Valid status values (enforced by CHECK constraint):
#   'draft'  — not yet active, editable
#   'active' — being dispatched by the scheduler
#   'paused' — temporarily stopped


def upgrade() -> None:
    logger.info("0049 upgrade: creating loyalty_campaigns table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS loyalty_campaigns (
            id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id            BIGINT      NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name              TEXT        NOT NULL,
            description       TEXT        NOT NULL DEFAULT '',
            segment           TEXT        NULL,
            trigger_type      TEXT        NOT NULL,
            message_template  TEXT        NOT NULL,
            status            TEXT        NOT NULL DEFAULT 'draft'
                CONSTRAINT chk_loyalty_campaigns_status
                CHECK (status IN ('draft', 'active', 'paused')),
            sent_count        INT         NOT NULL DEFAULT 0,
            converted_count   INT         NOT NULL DEFAULT 0,
            revenue_numeric   NUMERIC(14,2) NOT NULL DEFAULT 0,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    logger.info("0049 upgrade: creating index ix_loyalty_campaigns_org_status")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_loyalty_campaigns_org_status
        ON loyalty_campaigns (org_id, status)
    """)

    logger.info("0049 upgrade: enabling RLS on loyalty_campaigns")
    op.execute("ALTER TABLE loyalty_campaigns ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE loyalty_campaigns FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        DROP POLICY IF EXISTS org_isolation ON loyalty_campaigns;
        CREATE POLICY org_isolation ON loyalty_campaigns
        USING ({_ORG_ISOLATION_EXPR})
        WITH CHECK ({_ORG_ISOLATION_EXPR})
    """)

    logger.info("0049 upgrade: granting DML to mesio_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON loyalty_campaigns TO mesio_app"
    )

    logger.info("0049 upgrade: done")


def downgrade() -> None:
    logger.info("0049 downgrade: dropping loyalty_campaigns table")
    op.execute("DROP POLICY IF EXISTS org_isolation ON loyalty_campaigns")
    op.execute("DROP TABLE IF EXISTS loyalty_campaigns CASCADE")
    logger.info("0049 downgrade: done")
