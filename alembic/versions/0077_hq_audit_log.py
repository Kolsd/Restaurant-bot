"""Add hq_audit_log table for Mesio HQ compliance.

Every state-changing /api/internal/* call is recorded here for
accountability as the team grows beyond the founder.

The table is GLOBAL (no RLS) — accessed only via bypass_tenant_scope
from the audit middleware and the superadmin read endpoint.

Revision ID:  0077_hq_audit_log
Revises:      0076_rls_restaurant_tables
"""

import sqlalchemy as sa
from alembic import op

revision = "0077_hq_audit_log"
down_revision = "0076_rls_restaurant_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS hq_audit_log (
            id          BIGSERIAL PRIMARY KEY,
            actor       TEXT NOT NULL,
            action      TEXT NOT NULL,
            target_type TEXT,
            target_id   TEXT,
            org_id      BIGINT,
            payload     JSONB DEFAULT '{}'::jsonb,
            request_ip  TEXT,
            user_agent  TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_hq_audit_log_created_at "
        "ON hq_audit_log(created_at DESC)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_hq_audit_log_actor "
        "ON hq_audit_log(actor, created_at DESC)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_hq_audit_log_target "
        "ON hq_audit_log(target_type, target_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_hq_audit_log_org "
        "ON hq_audit_log(org_id, created_at DESC) WHERE org_id IS NOT NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS hq_audit_log"))
