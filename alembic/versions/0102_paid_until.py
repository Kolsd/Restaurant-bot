"""organizations.paid_until: the end of the period Mesio has been paid for.

Billing is manual (GTM decision 2026-09-23, docs/claude/status.md #14c):
Mesio invoices outside the product and records each payment in superadmin,
which moves paid_until forward a month or a year. Together with comp_until
(the free days) it gives the subscription state — trial, activo, vencido,
suspendido — computed in app/services/plans.billing_status. An org with
neither date is one Mesio manages by hand and stays active.

Revision ID: 0102_paid_until
Revises:     0101_pricing_per_sede
Create Date: 2026-09-30
"""
from alembic import op

revision = "0102_paid_until"
down_revision = "0101_pricing_per_sede"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS paid_until TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS paid_until")
