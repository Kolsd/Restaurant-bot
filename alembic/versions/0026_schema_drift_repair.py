"""Schema drift repair: columns the repos read/write but never existed in Alembic.

Found by running the real E2E simulator (tests/ai_sim) against a freshly-migrated
Postgres. In production, these columns were silently added at runtime or by hotfix
DDL and never formalised. Every fresh deploy (staging, sim, new Railway project)
was totally broken without this.

Columns added (all nullable, no data loss):
  - conversations.branch_id        INTEGER   (db_save_history INSERTs it)
  - conversations.restaurant_id    INTEGER   (restaurant_repo queries it)
  - nps_responses.branch_id        INTEGER   (repo INSERTs it)
  - nps_responses.restaurant_id    INTEGER   (reviews_repo / weekly_reports query it)
  - orders.restaurant_id           INTEGER   (restaurant_repo queries it)
  - orders.username                TEXT      (restaurant_repo queries it)
  - table_orders.bot_number        TEXT      (restaurant_repo queries it)
  - table_orders.restaurant_id     INTEGER   (queried by restaurant_repo, weekly_reports)
  - menu_availability.restaurant_id INTEGER  (db_get_menu_availability queries it)
  - waiter_alerts.status           TEXT      (tables_repo.py:1204,1271 writes it)

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-14
"""

from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade():
    # All ALTERs use IF NOT EXISTS so this is safe to re-run and safe on prod
    # DBs where the column was previously added by runtime DDL.
    op.execute("""
        ALTER TABLE conversations      ADD COLUMN IF NOT EXISTS branch_id      INTEGER;
        ALTER TABLE conversations      ADD COLUMN IF NOT EXISTS restaurant_id  INTEGER;

        ALTER TABLE nps_responses      ADD COLUMN IF NOT EXISTS branch_id      INTEGER;
        ALTER TABLE nps_responses      ADD COLUMN IF NOT EXISTS restaurant_id  INTEGER;

        ALTER TABLE orders             ADD COLUMN IF NOT EXISTS restaurant_id  INTEGER;
        ALTER TABLE orders             ADD COLUMN IF NOT EXISTS username       TEXT;

        ALTER TABLE table_orders       ADD COLUMN IF NOT EXISTS bot_number     TEXT;
        ALTER TABLE table_orders       ADD COLUMN IF NOT EXISTS restaurant_id  INTEGER;

        ALTER TABLE menu_availability  ADD COLUMN IF NOT EXISTS restaurant_id  INTEGER;

        ALTER TABLE waiter_alerts      ADD COLUMN IF NOT EXISTS status         TEXT;
    """)

    # Helpful indexes for lookups the repos actually perform.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_conversations_branch_id
            ON conversations(branch_id) WHERE branch_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_nps_responses_restaurant_id
            ON nps_responses(restaurant_id) WHERE restaurant_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_orders_restaurant_id
            ON orders(restaurant_id) WHERE restaurant_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_table_orders_restaurant_id
            ON table_orders(restaurant_id) WHERE restaurant_id IS NOT NULL;
    """)


def downgrade():
    # Columns were silently assumed to exist by many code paths. Dropping them
    # in downgrade is safe because Alembic never created them upstream anyway,
    # but we keep indexes out to minimise noise on rollback.
    op.execute("""
        ALTER TABLE conversations      DROP COLUMN IF EXISTS branch_id;
        ALTER TABLE conversations      DROP COLUMN IF EXISTS restaurant_id;
        ALTER TABLE nps_responses      DROP COLUMN IF EXISTS branch_id;
        ALTER TABLE nps_responses      DROP COLUMN IF EXISTS restaurant_id;
        ALTER TABLE orders             DROP COLUMN IF EXISTS restaurant_id;
        ALTER TABLE orders             DROP COLUMN IF EXISTS username;
        ALTER TABLE table_orders       DROP COLUMN IF EXISTS bot_number;
        ALTER TABLE table_orders       DROP COLUMN IF EXISTS restaurant_id;
        ALTER TABLE menu_availability  DROP COLUMN IF EXISTS restaurant_id;
        ALTER TABLE waiter_alerts      DROP COLUMN IF EXISTS status;
    """)
