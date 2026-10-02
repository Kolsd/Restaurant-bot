"""platform_errors: every 500 and every bot failure, with its org and sede.

Mesio HQ wave 2. A diner whose bot turn failed got "tengo un problema
técnico" and Mesio never knew; a 500 lived only in the process logs. Each
row says which restaurant and sede it hit, so the ficha can show it and the
alert rules (wave 4) can count it. Mesio-internal like hq_audit_log: written
and read under bypass_tenant_scope only, never by a restaurant.

`fingerprint` groups repeats of the same failure (source + route + type +
message with numbers stripped). Rows older than 90 days are purged by the
scheduler.

Revision ID: 0108_platform_errors
Revises:     0107_table_orders_ready_at
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0108_platform_errors"
down_revision = "0107_table_orders_ready_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS platform_errors (
            id          BIGSERIAL PRIMARY KEY,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source      TEXT NOT NULL,
            org_id      BIGINT,
            location_id BIGINT,
            route       TEXT,
            method      TEXT,
            status      INTEGER,
            error_type  TEXT NOT NULL,
            message     TEXT,
            fingerprint TEXT NOT NULL,
            request_id  TEXT
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_platform_errors_created ON platform_errors (created_at DESC)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_platform_errors_org "
        "ON platform_errors (org_id, created_at DESC) WHERE org_id IS NOT NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_platform_errors_fingerprint ON platform_errors (fingerprint, created_at DESC)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS platform_errors"))
