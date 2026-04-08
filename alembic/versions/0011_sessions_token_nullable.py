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
    # token is PRIMARY KEY — PK columns cannot be nullable.
    # Step 1: add a surrogate PK so token can be demoted.
    op.execute("""
    ALTER TABLE sessions
        ADD COLUMN IF NOT EXISTS id BIGSERIAL
    """)
    # Step 2: drop the token primary key constraint.
    op.execute("""
    ALTER TABLE sessions
        DROP CONSTRAINT IF EXISTS sessions_pkey
    """)
    # Step 3: promote id to primary key.
    op.execute("""
    ALTER TABLE sessions
        ADD PRIMARY KEY (id)
    """)
    # Step 4: now token has no PK obligation — drop NOT NULL.
    op.execute("""
    ALTER TABLE sessions
        ALTER COLUMN token DROP NOT NULL
    """)


def downgrade() -> None:
    # Restore token values where missing, then reinstate token as PK.
    op.execute("""
    UPDATE sessions SET token = encode(token_hash, 'hex')
     WHERE token IS NULL AND token_hash IS NOT NULL
    """)
    op.execute("""
    ALTER TABLE sessions
        DROP CONSTRAINT IF EXISTS sessions_pkey
    """)
    op.execute("""
    ALTER TABLE sessions
        ALTER COLUMN token SET NOT NULL
    """)
    op.execute("""
    ALTER TABLE sessions
        ADD PRIMARY KEY (token)
    """)
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS id")
