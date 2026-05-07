"""Add pending plan downgrade fields to organizations.

When a restaurant requests a downgrade, three nullable columns track the
scheduled transition.  On the effective date the scheduler applies the
change and clears the fields.

  pending_plan_code         TEXT     — the plan they're moving to
  pending_plan_effective_at TIMESTAMPTZ — when it takes effect (NOW()+7d)
  pending_kept_location_id  BIGINT   — which location they chose to keep

All three are NULL when no downgrade is pending.  Idempotent (IF NOT EXISTS).

Revision ID:  0075_pending_plan_downgrade
Revises:      0074_conv_turns_without_progress
"""

import sqlalchemy as sa
from alembic import op

revision = "0075_pending_plan_downgrade"
down_revision = "0074_conv_turns_without_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS pending_plan_code TEXT"
    ))
    op.execute(sa.text(
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS pending_plan_effective_at TIMESTAMPTZ"
    ))
    op.execute(sa.text(
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS pending_kept_location_id BIGINT"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS pending_kept_location_id"
    ))
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS pending_plan_effective_at"
    ))
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS pending_plan_code"
    ))
