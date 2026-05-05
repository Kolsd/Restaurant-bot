"""Password reset tokens — 6-digit code flow via WhatsApp.

Adds one table:
  - password_reset_tokens: stores hashed OTP codes for password recovery.

Schema note: users.username is TEXT PRIMARY KEY (not an integer id).
             We reference users(username) accordingly.

This table is GLOBAL (no restaurant_id / org_id) — same class as
`sessions` and `webhook_inbox`. NOT added to _RLS_TABLES.

Revision ID: 0069_password_reset_tokens
Revises: 0068_stats_hot_path_indexes
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0069_password_reset_tokens"
down_revision = "0068_stats_hot_path_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    logger.info("0069 upgrade: creating password_reset_tokens table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id           BIGSERIAL    PRIMARY KEY,
            user_username TEXT        NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            code_hash    BYTEA        NOT NULL,
            expires_at   TIMESTAMPTZ  NOT NULL,
            used_at      TIMESTAMPTZ  NULL,
            attempts     INT          NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    logger.info("0069 upgrade: creating index ix_password_reset_user_active")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_password_reset_user_active
        ON password_reset_tokens (user_username)
        WHERE used_at IS NULL
    """)

    logger.info("0069 upgrade: creating index ix_password_reset_expires_at")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_password_reset_expires_at
        ON password_reset_tokens (expires_at)
    """)

    # Grant DML access to the runtime user (mesio_app).
    # No RLS needed — global table keyed by user_username, no org boundary.
    logger.info("0069 upgrade: granting DML to mesio_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_tokens TO mesio_app"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE password_reset_tokens_id_seq TO mesio_app"
    )

    logger.info("0069 upgrade: done")


def downgrade() -> None:
    logger.info("0069 downgrade: dropping password_reset_tokens table")
    op.execute("DROP INDEX IF EXISTS ix_password_reset_expires_at")
    op.execute("DROP INDEX IF EXISTS ix_password_reset_user_active")
    op.execute("DROP TABLE IF EXISTS password_reset_tokens CASCADE")
    logger.info("0069 downgrade: done")
