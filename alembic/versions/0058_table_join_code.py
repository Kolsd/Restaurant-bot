"""
0058 — table_sessions.join_code: Capa 2 multi-participant join code.

Background:
  When the first customer scans a table QR and opens a session (host),
  the bot generates a 4-digit numeric join code and stores it in the session.
  Subsequent participants who scan the same table QR are asked to enter that
  code before a new session is opened for them.

  Full design in docs/MESA_QR_ARCHITECTURE.md, section "Capa 2".

Schema change:
  ALTER TABLE table_sessions ADD COLUMN join_code TEXT NULL;

  The column is NULL for sessions created before this migration and for
  non-QR-claim sessions (path 1 / marker-based fallback). It is set
  atomically when the host opens a new session (db_set_session_join_code),
  guaranteeing only one code per active session via the WHERE join_code IS NULL
  guard.

Index:
  ix_table_sessions_join_code — partial index on non-NULL join_code to
  support O(1) lookup by code during participant join validation
  (db_link_participant_session). The partial WHERE keeps the index small
  since the majority of historical sessions have join_code = NULL.

No RLS change: table_sessions already has org_isolation policy from
migration 0029 / Wave-2 updates. The new column inherits that protection.

Revision ID: 0058
Revises: 0057
Create Date: 2026-04-29
"""

from alembic import op


revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE table_sessions
        ADD COLUMN IF NOT EXISTS join_code TEXT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_table_sessions_join_code
            ON table_sessions (join_code)
            WHERE join_code IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_table_sessions_join_code;")
    op.execute("ALTER TABLE table_sessions DROP COLUMN IF EXISTS join_code;")
