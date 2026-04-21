"""shift_swap_requests: add CHECK constraint on status column.

GAP CLOSED:
  shift_swap_requests.status is a free-form TEXT column (migration 0043).
  Any string value is accepted, making the state machine non-enforced at the
  DB level. This migration adds a CHECK constraint locking the column to the
  six valid states documented in CLAUDE.md.

VALID STATES (from Sprint W state machine):
  pending           — initial state, awaiting target staff response
  target_accepted   — target accepted, awaiting admin approval
  target_rejected   — target declined
  admin_approved    — admin approved the swap
  admin_rejected    — admin rejected the swap
  withdrawn         — requester cancelled their own request

PRE-FLIGHT CLEANUP:
  Any existing rows with invalid status values are updated to 'pending' before
  the constraint is added. A NOTICE is logged for each affected row count so
  that operators can audit post-migration if needed.

DOWNGRADE:
  Simply drops the CHECK constraint — no data change.

Revision ID: 0047_swap_status_check
Revises: 0046_money_precision
Create Date: 2026-04-20
"""

import logging

from alembic import op

revision = "0047_swap_status_check"
down_revision = "0046_money_precision"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_VALID_STATUSES = (
    "pending",
    "target_accepted",
    "target_rejected",
    "admin_approved",
    "admin_rejected",
    "withdrawn",
)

_CONSTRAINT_NAME = "chk_shift_swap_status"


def upgrade() -> None:
    logger.info("0047 upgrade: pre-flight cleanup of invalid shift_swap_requests.status values")

    # Sanitize any invalid rows before adding the constraint
    valid_list = ", ".join(f"'{s}'" for s in _VALID_STATUSES)
    op.execute(f"""
        DO $$
        DECLARE invalid_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO invalid_count
            FROM shift_swap_requests
            WHERE status NOT IN ({valid_list});

            IF invalid_count > 0 THEN
                RAISE NOTICE '0047: found % shift_swap_requests rows with invalid status — resetting to ''pending''', invalid_count;
                UPDATE shift_swap_requests
                SET status = 'pending'
                WHERE status NOT IN ({valid_list});
            END IF;
        END $$;
    """)

    logger.info("0047 upgrade: adding CHECK constraint %s", _CONSTRAINT_NAME)
    op.execute(f"""
        ALTER TABLE shift_swap_requests
        ADD CONSTRAINT {_CONSTRAINT_NAME}
        CHECK (status IN ({valid_list}))
    """)

    logger.info("0047 upgrade: CHECK constraint added successfully")


def downgrade() -> None:
    logger.info("0047 downgrade: dropping CHECK constraint %s", _CONSTRAINT_NAME)
    op.execute(f"""
        ALTER TABLE shift_swap_requests
        DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}
    """)
    logger.info("0047 downgrade: done")
