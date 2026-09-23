"""prospects: the CRM table never had its columns on a DB built from scratch.

Migration 0012 creates a four-column STUB of `prospects` (id, phone, name,
created_at) so that a REFERENCES prospects(id) foreign key in
`sales_conversations` has something to point at. Migration 0020 then does

    CREATE TABLE IF NOT EXISTS prospects (... the real CRM schema ...)

and IF NOT EXISTS makes that a silent no-op, because 0012 already created
the table. Every database built from empty therefore ends up with the stub,
and the whole CRM is broken against it: `db_create_prospect` inserts
restaurant_name / owner_name / city / source / stage / tags, none of which
exist, so filing a lead raises UndefinedColumnError. The public signup
endpoint caught that as a generic failure and answered 500 — which is a
large part of why leads "had to be entered by hand".

0078 later added lost_reason / lost_at with ALTER TABLE, and those two DO
exist, which is the fingerprint of the bug: the patches landed, the table
body never did.

This migration converges both shapes with ADD COLUMN IF NOT EXISTS, so it
is a no-op on a database that happened to get the 0020 body and a repair on
one that got the stub. Every added column carries a DEFAULT because the
table may already hold rows, and `restaurant_name` is backfilled from the
stub's `name` rather than left blank — on a stub-built database that column
is the only record of who the prospect was.

Revision ID: 0095_prospects_full_schema
Revises:     0094_token_usage_breakdown
Create Date: 2026-09-23
"""

from alembic import op

revision = "0095_prospects_full_schema"
down_revision = "0094_token_usage_breakdown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS restaurant_name TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS owner_name      TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS city            TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS neighborhood    TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS category        TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS instagram       TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS google_maps     TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS source          TEXT NOT NULL DEFAULT 'manual'")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS stage           TEXT NOT NULL DEFAULT 'prospecto'")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS priority        TEXT NOT NULL DEFAULT 'medium'")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS assigned_to     TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS last_contact_at TIMESTAMP")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS next_follow_up  TIMESTAMP")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS revenue_est     INTEGER DEFAULT 0")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS tags            TEXT[] DEFAULT '{}'")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS archived        BOOLEAN DEFAULT FALSE")
    op.execute("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMP DEFAULT NOW()")

    # On a stub-built database `name` holds whatever the prospect was
    # called; that is the CRM's restaurant_name and nothing else carries it.
    op.execute("""
        UPDATE prospects
           SET restaurant_name = COALESCE(NULLIF(name, ''), restaurant_name)
         WHERE restaurant_name = ''
    """)

    # The CRM lists by stage and by last touch; without these every board
    # refresh is a sequential scan of the whole table.
    op.execute("CREATE INDEX IF NOT EXISTS idx_prospects_stage ON prospects(stage)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_prospects_phone ON prospects(phone)")


def downgrade() -> None:
    # Deliberately not dropping the columns: on a database that took the
    # 0020 body they are the original schema, and dropping them here would
    # destroy real CRM data to undo a migration that changed nothing there.
    op.execute("DROP INDEX IF EXISTS idx_prospects_phone")
    op.execute("DROP INDEX IF EXISTS idx_prospects_stage")
