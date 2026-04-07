"""Session token hashing — store only SHA-256 digest in DB.

Adds token_hash (BYTEA) to the sessions table and backfills existing rows
using pgcrypto.  The raw token column is left in place (nullable) so that
existing logged-in users are not forcibly kicked out during the rollout.
Drop `token` in a follow-up migration once legacy_lookup log volume reaches zero.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-07
"""
from alembic import op

revision      = "0010"
down_revision = "0009"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # pgcrypto is needed for the backfill digest().
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # Add the new column (nullable — new rows set token=NULL, token_hash=<digest>).
    op.execute("""
    ALTER TABLE sessions
        ADD COLUMN IF NOT EXISTS token_hash BYTEA
    """)

    # Backfill existing plaintext rows.
    # Rows that already have token_hash (re-running after partial migration) are skipped.
    op.execute("""
    UPDATE sessions
       SET token_hash = digest(token, 'sha256')
     WHERE token_hash IS NULL
       AND token IS NOT NULL
    """)

    # Unique index for fast lookup by hash.
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_token_hash
        ON sessions (token_hash)
        WHERE token_hash IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_sessions_token_hash")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS token_hash")
