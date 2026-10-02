"""hq_alerts: Mesio HQ alerts that open, stay and close on their own.

Mesio HQ wave 4. The scheduler evaluates every organization's health flags
(services/hq_snapshot.RUNBOOK) every few minutes: a flag that appears opens
an alert, one that is still there bumps last_seen, one that is gone is
resolved. Critical alerts are emailed once when they open (emailed_at), not
every tick — the old in-memory cooldown repeated after every restart.

`key` = code + org + sede: at most one OPEN alert per key (partial unique
index). Mesio-internal like hq_audit_log: bypass_tenant_scope only.

Revision ID: 0109_hq_alerts
Revises:     0108_platform_errors
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0109_hq_alerts"
down_revision = "0108_platform_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS hq_alerts (
            id           BIGSERIAL PRIMARY KEY,
            key          TEXT NOT NULL,
            code         TEXT NOT NULL,
            severity     TEXT NOT NULL,
            org_id       BIGINT,
            location_id  BIGINT,
            title        TEXT NOT NULL,
            detail       TEXT,
            count        INTEGER,
            status       TEXT NOT NULL DEFAULT 'open',
            opened_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at  TIMESTAMPTZ,
            emailed_at   TIMESTAMPTZ
        )
    """))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_hq_alerts_open_key ON hq_alerts (key) WHERE status = 'open'"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_hq_alerts_org ON hq_alerts (org_id, opened_at DESC)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_hq_alerts_status ON hq_alerts (status, severity, opened_at DESC)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS hq_alerts"))
