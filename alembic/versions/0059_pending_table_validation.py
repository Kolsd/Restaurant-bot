"""
0059 — pending_table_validation: Capa 3 anti-impostor (first order hold).

Background:
  Capa 3 of the Mesa QR Architecture (docs/MESA_QR_ARCHITECTURE.md).
  When a new table session is not yet verified by a waiter, the first order
  the customer places is held with pending_table_validation=true.  The waiter
  physically confirms the table (→ confirm-real) or marks it a ghost
  (→ mark-ghost).  Once confirmed, table_sessions.verified=true and all
  subsequent orders go straight to the kitchen.

Schema changes:
  table_orders.pending_table_validation BOOLEAN NOT NULL DEFAULT false
    Partial index ix_table_orders_pending_validation for fast mesero dashboard
    queries (WHERE pending_table_validation = true).

  table_sessions.verified BOOLEAN NOT NULL DEFAULT false
    true  after waiter taps "Confirmar mesa real".
    false on new sessions — triggers the validation hold on the first order.

No RLS change: both tables already carry org_isolation policies.

Revision ID: 0059
Revises: 0058
Create Date: 2026-04-29
"""

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE table_orders
        ADD COLUMN IF NOT EXISTS pending_table_validation BOOLEAN NOT NULL DEFAULT false;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_table_orders_pending_validation
            ON table_orders (pending_table_validation, created_at)
            WHERE pending_table_validation = true;
        """
    )
    op.execute(
        """
        ALTER TABLE table_sessions
        ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_table_orders_pending_validation;")
    op.execute(
        "ALTER TABLE table_orders DROP COLUMN IF EXISTS pending_table_validation;"
    )
    op.execute(
        "ALTER TABLE table_sessions DROP COLUMN IF EXISTS verified;"
    )
