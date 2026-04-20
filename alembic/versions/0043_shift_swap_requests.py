"""Shift swap requests: staff↔staff turn exchanges with admin approval.

Adds one table:
  - shift_swap_requests: full lifecycle (pending → target_accepted/rejected
    → admin_approved/rejected / withdrawn).

RLS: org_isolation policy (same pattern as 0042_staff_comms).

Revision ID: 0043_shift_swap
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0043_shift_swap"
down_revision = "0042_staff_comms"
branch_labels = None
depends_on = None

_ORG_ISOLATION_EXPR = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)


def upgrade() -> None:
    logger.info("0043 upgrade: creating shift_swap_requests table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS shift_swap_requests (
            id                  BIGSERIAL PRIMARY KEY,
            org_id              BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            requester_staff_id  UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            target_staff_id     UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            shift_date          DATE NOT NULL,
            shift_start_time    TIME NULL,
            shift_end_time      TIME NULL,
            reason              TEXT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            decided_by          TEXT NULL,
            decided_at          TIMESTAMPTZ NULL,
            admin_notes         TEXT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    logger.info("0043 upgrade: creating indexes")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_shift_swap_org_status
        ON shift_swap_requests (org_id, status, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_shift_swap_target_pending
        ON shift_swap_requests (target_staff_id)
        WHERE status = 'pending'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_shift_swap_requester
        ON shift_swap_requests (requester_staff_id, created_at DESC)
    """)

    logger.info("0043 upgrade: enabling RLS on shift_swap_requests")
    op.execute("ALTER TABLE shift_swap_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shift_swap_requests FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation_org ON shift_swap_requests
        USING ({_ORG_ISOLATION_EXPR})
        WITH CHECK ({_ORG_ISOLATION_EXPR})
    """)

    logger.info("0043 upgrade: done")


def downgrade() -> None:
    logger.info("0043 downgrade: dropping shift_swap_requests table")
    op.execute("DROP TABLE IF EXISTS shift_swap_requests CASCADE")
    logger.info("0043 downgrade: done")
