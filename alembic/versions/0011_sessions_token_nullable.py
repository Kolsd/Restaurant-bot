"""Make sessions.token nullable — required after token-hash migration.

The 0010 migration added token_hash and backfilled existing rows, but did not
relax the NOT NULL constraint on the legacy `token` column.  New sessions now
store only the SHA-256 hash, so `token` must be nullable until it is dropped.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-08
"""
from alembic import op

revision      = "0011"
down_revision = "0010"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE sessions
        ALTER COLUMN token DROP NOT NULL
    """)


def downgrade() -> None:
    # Only safe if no rows have token IS NULL; guard with a check.
    op.execute("""
    UPDATE sessions SET token = encode(token_hash, 'hex')
     WHERE token IS NULL AND token_hash IS NOT NULL
    """)
    op.execute("""
    ALTER TABLE sessions
        ALTER COLUMN token SET NOT NULL
    """)
