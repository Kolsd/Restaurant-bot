"""
0053 — Add nps_responses.table_session_id FK to enable per-mesero NPS aggregation.

Background:
  Prior to this, nps_responses only carried phone + bot_number + branch_id. To compute
  an NPS average per waiter, we need a join path to table_sessions (which carries
  assigned_staff_id post migration 0039) or to table_orders (waiter_staff_id). A FK to
  table_sessions is the natural choice because:
    - table_sessions is the unit that represents "the customer sat down and ate"
    - assigned_staff_id on table_sessions is the waiter who owned the shift

Schema change:
  nps_responses.table_session_id INTEGER NULL REFERENCES table_sessions(id) ON DELETE SET NULL

  (table_sessions.id is SERIAL PRIMARY KEY — integer, NOT UUID. Do not change to UUID.)

NULL semantics: old rows (pre-0053) have NULL; delivery/pickup NPS also NULL because
there is no table_session. Only dine-in salon NPS will get populated.

Index: partial index WHERE table_session_id IS NOT NULL for staff-performance JOIN.

Revision ID: 0053
Revises: 0052
Create Date: 2026-04-24
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE nps_responses
        ADD COLUMN IF NOT EXISTS table_session_id INTEGER NULL
    """)

    # FK constraint — CASCADE SET NULL so that deleting a session doesn't lose NPS data.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_name = 'nps_responses'
                  AND constraint_name = 'fk_nps_responses_table_session_id'
            ) THEN
                ALTER TABLE nps_responses
                ADD CONSTRAINT fk_nps_responses_table_session_id
                FOREIGN KEY (table_session_id)
                REFERENCES table_sessions(id)
                ON DELETE SET NULL;
            END IF;
        END$$;
    """)

    # Partial index: only index rows where the FK is populated (salon NPS).
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_nps_responses_table_session_id
        ON nps_responses (table_session_id)
        WHERE table_session_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_nps_responses_table_session_id")
    op.execute("""
        ALTER TABLE nps_responses
        DROP CONSTRAINT IF EXISTS fk_nps_responses_table_session_id
    """)
    op.execute("ALTER TABLE nps_responses DROP COLUMN IF EXISTS table_session_id")
