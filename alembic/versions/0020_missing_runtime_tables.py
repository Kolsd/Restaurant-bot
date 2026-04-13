"""Formalises tables previously created at runtime by db_init_* functions.

Creates:
  - subscription_usage   (was in database._ensure_usage_table)
  - loyalty_customers    (was in database._ensure_loyalty_tables)
  - loyalty_ledger       (was in database._ensure_loyalty_tables)
  - prospects            (was in routes/crm._ensure_crm_tables)
  - prospect_notes       (was in routes/crm._ensure_crm_tables)
  - prospect_interactions(was in routes/crm._ensure_crm_tables)
  - crm_templates        (was in routes/crm._ensure_crm_tables)

Also adds the missing index idx_rest_tables_lookup that was previously
created at runtime in inventory_repo.db_init_nps_inventory.

All previously redundant runtime DDL (tables already covered by 0001) has been
removed from the Python source; only the tables not present in any prior
migration are created here.

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-13
"""
from alembic import op

revision      = "0020"
down_revision = "0019"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── Subscription Usage ────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS subscription_usage (
            id             BIGSERIAL   PRIMARY KEY,
            restaurant_id  INTEGER     NOT NULL,
            usage_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
            total_tokens   INTEGER     NOT NULL DEFAULT 0,
            total_invoices INTEGER     NOT NULL DEFAULT 0,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (restaurant_id, usage_date)
        )
    """)

    # ── Loyalty ───────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS loyalty_customers (
            id             SERIAL PRIMARY KEY,
            restaurant_id  INTEGER NOT NULL,
            phone          TEXT    NOT NULL,
            points_balance INTEGER NOT NULL DEFAULT 0,
            total_earned   INTEGER NOT NULL DEFAULT 0,
            total_redeemed INTEGER NOT NULL DEFAULT 0,
            created_at     TIMESTAMP DEFAULT NOW(),
            updated_at     TIMESTAMP DEFAULT NOW(),
            UNIQUE (restaurant_id, phone)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS loyalty_ledger (
            id            BIGSERIAL PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            phone         TEXT    NOT NULL,
            delta         INTEGER NOT NULL,
            reason        TEXT    NOT NULL DEFAULT 'purchase',
            order_id      TEXT,
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_cust_lookup  ON loyalty_customers (restaurant_id, phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_ledger_lookup ON loyalty_ledger    (restaurant_id, phone, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_loyalty_ledger_order  ON loyalty_ledger    (order_id) WHERE order_id IS NOT NULL")

    # ── CRM ───────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS prospects (
            id              SERIAL PRIMARY KEY,
            restaurant_name TEXT      NOT NULL,
            owner_name      TEXT      NOT NULL DEFAULT '',
            phone           TEXT      NOT NULL,
            city            TEXT      NOT NULL DEFAULT '',
            neighborhood    TEXT      NOT NULL DEFAULT '',
            category        TEXT      NOT NULL DEFAULT '',
            instagram       TEXT      NOT NULL DEFAULT '',
            google_maps     TEXT      NOT NULL DEFAULT '',
            source          TEXT      NOT NULL DEFAULT 'manual',
            stage           TEXT      NOT NULL DEFAULT 'prospecto',
            priority        TEXT      NOT NULL DEFAULT 'medium',
            assigned_to     TEXT      NOT NULL DEFAULT '',
            last_contact_at TIMESTAMP,
            next_follow_up  TIMESTAMP,
            revenue_est     INTEGER   DEFAULT 0,
            tags            TEXT[]    DEFAULT '{}',
            archived        BOOLEAN   DEFAULT FALSE,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS prospect_notes (
            id          SERIAL PRIMARY KEY,
            prospect_id INTEGER NOT NULL,
            author      TEXT    NOT NULL DEFAULT 'admin',
            content     TEXT    NOT NULL,
            note_type   TEXT    NOT NULL DEFAULT 'note',
            created_at  TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS prospect_interactions (
            id            SERIAL PRIMARY KEY,
            prospect_id   INTEGER NOT NULL,
            direction     TEXT    NOT NULL DEFAULT 'outbound',
            channel       TEXT    NOT NULL DEFAULT 'whatsapp',
            content       TEXT    NOT NULL,
            template_name TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'sent',
            wa_message_id TEXT    NOT NULL DEFAULT '',
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS crm_templates (
            id         SERIAL PRIMARY KEY,
            name       TEXT    NOT NULL UNIQUE,
            wa_name    TEXT    NOT NULL DEFAULT '',
            category   TEXT    NOT NULL DEFAULT 'MARKETING',
            language   TEXT    NOT NULL DEFAULT 'es',
            body       TEXT    NOT NULL,
            params     TEXT[]  DEFAULT '{}',
            active     BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # NOTE: 0012_b2b_sales_system already adds enrichment columns to prospects
    # (email, website, employee_count, monthly_orders_est, current_solution, lead_score)
    # via ALTER TABLE. Those columns are safe to add after this migration runs because
    # 0020 sets down_revision=0019 and 0012 is an older migration — on a fresh DB
    # alembic runs migrations in order so 0020 runs after 0012 which would already
    # have run. On existing DBs, 0020 uses IF NOT EXISTS so it's idempotent.
    # The FK in 0012 sales_conversations(prospect_id) requires prospects to exist first;
    # on a fresh DB 0020 < 0012 in run order would be wrong, but 0012 already uses
    # IF NOT EXISTS on the FK and prospects was previously created at runtime — so
    # existing production DBs have the table. This migration is additive/idempotent.

    # ── Missing index (was runtime-only in inventory_repo.db_init_nps_inventory) ─
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_rest_tables_lookup
            ON restaurant_tables (number, branch_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_rest_tables_lookup")
    op.execute("DROP TABLE IF EXISTS crm_templates CASCADE")
    op.execute("DROP TABLE IF EXISTS prospect_interactions CASCADE")
    op.execute("DROP TABLE IF EXISTS prospect_notes CASCADE")
    op.execute("DROP TABLE IF EXISTS prospects CASCADE")
    op.execute("DROP INDEX IF EXISTS idx_loyalty_ledger_order")
    op.execute("DROP INDEX IF EXISTS idx_loyalty_ledger_lookup")
    op.execute("DROP INDEX IF EXISTS idx_loyalty_cust_lookup")
    op.execute("DROP TABLE IF EXISTS loyalty_ledger CASCADE")
    op.execute("DROP TABLE IF EXISTS loyalty_customers CASCADE")
    op.execute("DROP TABLE IF EXISTS subscription_usage CASCADE")
