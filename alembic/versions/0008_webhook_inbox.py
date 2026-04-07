"""Webhook inbox — durable inbox table for inbound webhook events.

Creates:
  - webhook_inbox: DB-backed queue for inbound Meta/Wompi webhooks
    with idempotency dedup, exponential-backoff retry, and dead-letter support.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-07
"""
from alembic import op

revision      = "0008"
down_revision = "0007"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── Webhook inbox table ───────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS webhook_inbox (
        id              BIGSERIAL PRIMARY KEY,
        provider        TEXT NOT NULL,
        external_id     TEXT,
        payload         JSONB NOT NULL,
        received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at    TIMESTAMPTZ,
        attempts        INT NOT NULL DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)

    # Partial index for polling pending rows efficiently
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_webhook_inbox_pending
        ON webhook_inbox (next_attempt_at)
        WHERE processed_at IS NULL
    """)

    # Unique partial index for idempotency dedup
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_inbox_dedup
        ON webhook_inbox (provider, external_id)
        WHERE external_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_webhook_inbox_dedup")
    op.execute("DROP INDEX IF EXISTS ix_webhook_inbox_pending")
    op.execute("DROP TABLE IF EXISTS webhook_inbox CASCADE")
