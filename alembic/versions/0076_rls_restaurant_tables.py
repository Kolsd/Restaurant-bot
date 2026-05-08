"""Enable RLS on restaurant_tables.

restaurant_tables stores per-tenant mesa layout (capacity, type, zone,
floor-plan positions) and was the only operational table in the RLS
surface area that still lacked a policy.  Without this, any mesio_app
query against restaurant_tables returns ALL tables across ALL tenants
when no org_id GUC is set — i.e., the table falls back to Postgres's
default-permit behaviour despite FORCE RLS on sibling tables.

This migration mirrors the pattern used for the other 38 RLS-enabled
tables (0029 / 0030 + wave-2 org_isolation policies).

Revision ID:  0076_rls_restaurant_tables
Revises:      0075_pending_plan_downgrade
"""

import sqlalchemy as sa
from alembic import op

revision = "0076_rls_restaurant_tables"
down_revision = "0075_pending_plan_downgrade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: IF NOT EXISTS / IF EXISTS guards allow safe re-runs.
    op.execute(sa.text(
        "ALTER TABLE restaurant_tables ENABLE ROW LEVEL SECURITY"
    ))
    op.execute(sa.text(
        "ALTER TABLE restaurant_tables FORCE ROW LEVEL SECURITY"
    ))
    # Drop first so the migration is re-runnable (e.g. after a downgrade).
    op.execute(sa.text(
        "DROP POLICY IF EXISTS org_isolation ON restaurant_tables"
    ))
    op.execute(sa.text(
        """
        CREATE POLICY org_isolation ON restaurant_tables
          USING (
            org_id = NULLIF(current_setting('app.org_id', true), '')::int
          )
          WITH CHECK (
            org_id = NULLIF(current_setting('app.org_id', true), '')::int
          )
        """
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP POLICY IF EXISTS org_isolation ON restaurant_tables"
    ))
    op.execute(sa.text(
        "ALTER TABLE restaurant_tables NO FORCE ROW LEVEL SECURITY"
    ))
    op.execute(sa.text(
        "ALTER TABLE restaurant_tables DISABLE ROW LEVEL SECURITY"
    ))
