"""Drop orphan B2B sales tables (migration 0012 cleanup).

The B2B sales agent project was cancelled and `sales_agent.py` was deleted
in Fase 7 cleanup. The four tables it created in migration 0012 have had
zero Python references since then. This migration drops them (and their
indexes) so the schema matches what the app actually uses.

Scope — only the sales_* tables are dropped here. The enrichment columns
added to `prospects` in 0012 (email, website, employee_count,
monthly_orders_est, current_solution, lead_score) are left in place; the
active CRM may still reference them and any cleanup there needs its own
audit.

Revision ID: 0031_drop_orphan_sales_tables
Revises: 0030_force_rls
Create Date: 2026-04-16
"""
from alembic import op

revision      = "0031_drop_orphan_sales_tables"
down_revision = "0030_force_rls"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # sales_escalations references sales_conversations, so drop it first.
    op.execute("DROP INDEX IF EXISTS ix_sales_esc_pending")
    op.execute("DROP TABLE IF EXISTS sales_escalations CASCADE")

    op.execute("DROP INDEX IF EXISTS ix_sales_conv_escalated")
    op.execute("DROP INDEX IF EXISTS ix_sales_conv_prospect")
    op.execute("DROP INDEX IF EXISTS ix_sales_conv_phone")
    op.execute("DROP TABLE IF EXISTS sales_conversations CASCADE")

    op.execute("DROP INDEX IF EXISTS ix_skb_category_active")
    op.execute("DROP TABLE IF EXISTS sales_knowledge_base CASCADE")

    op.execute("DROP INDEX IF EXISTS ux_sales_inbox_dedup")
    op.execute("DROP INDEX IF EXISTS ix_sales_inbox_pending")
    op.execute("DROP TABLE IF EXISTS sales_inbox CASCADE")


def downgrade() -> None:
    # Recreate the minimum shape needed to roll back. The knowledge base
    # seed rows from 0012 are NOT re-seeded — this downgrade restores
    # schema only.

    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_inbox (
        id              BIGSERIAL PRIMARY KEY,
        external_id     TEXT,
        payload         JSONB NOT NULL,
        received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at    TIMESTAMPTZ,
        attempts        INT NOT NULL DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_inbox_pending
        ON sales_inbox (next_attempt_at)
        WHERE processed_at IS NULL
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_inbox_dedup
        ON sales_inbox (external_id)
        WHERE external_id IS NOT NULL
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_knowledge_base (
        id         SERIAL PRIMARY KEY,
        category   TEXT NOT NULL,
        title      TEXT NOT NULL,
        content    TEXT NOT NULL,
        priority   INT  NOT NULL DEFAULT 0,
        active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_skb_category_active
        ON sales_knowledge_base (category, active)
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_conversations (
        id              BIGSERIAL PRIMARY KEY,
        prospect_id     INT REFERENCES prospects(id) ON DELETE SET NULL,
        phone           TEXT NOT NULL,
        channel         TEXT NOT NULL DEFAULT 'whatsapp',
        messages        JSONB NOT NULL DEFAULT '[]'::jsonb,
        agent_state     TEXT NOT NULL DEFAULT 'greeting',
        context         JSONB NOT NULL DEFAULT '{}'::jsonb,
        escalation      TEXT,
        escalated_at    TIMESTAMPTZ,
        resolved_at     TIMESTAMPTZ,
        token_count     INT NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_conv_phone
        ON sales_conversations (phone)
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_conv_prospect
        ON sales_conversations (prospect_id)
        WHERE prospect_id IS NOT NULL
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_conv_escalated
        ON sales_conversations (escalated_at)
        WHERE escalation IS NOT NULL AND resolved_at IS NULL
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS sales_escalations (
        id               SERIAL PRIMARY KEY,
        conversation_id  BIGINT NOT NULL REFERENCES sales_conversations(id) ON DELETE CASCADE,
        prospect_id      INT REFERENCES prospects(id) ON DELETE SET NULL,
        reason           TEXT NOT NULL,
        agent_summary    TEXT NOT NULL,
        suggested_action TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
        assigned_to      TEXT,
        resolution_note  TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at      TIMESTAMPTZ
    )
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_sales_esc_pending
        ON sales_escalations (created_at)
        WHERE status = 'pending'
    """)
