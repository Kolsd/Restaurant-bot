"""Payroll — contract templates, staff contract columns, overtime requests.

Creates:
  - contract_templates  (UUID PK) — reusable payroll contract definitions per restaurant
  - overtime_requests   (UUID PK) — weekly overtime approval workflow per staff member

Alters:
  - staff: adds contract_template_id, contract_overrides, contract_start

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-06
"""
from alembic import op

revision      = "0007"
down_revision = "0006"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── Contract templates ────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS contract_templates (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        restaurant_id       INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
        name                VARCHAR(100) NOT NULL,
        weekly_hours        NUMERIC(5,2) NOT NULL DEFAULT 44,
        monthly_salary      NUMERIC(14,2) NOT NULL DEFAULT 0,
        pay_period          TEXT NOT NULL DEFAULT 'biweekly' CHECK (pay_period IN ('monthly','biweekly','weekly')),
        transport_subsidy   NUMERIC(14,2) NOT NULL DEFAULT 0,
        arl_pct             NUMERIC(5,4) NOT NULL DEFAULT 0.00522,
        health_pct          NUMERIC(5,4) NOT NULL DEFAULT 0.04,
        pension_pct         NUMERIC(5,4) NOT NULL DEFAULT 0.04,
        other_benefits      JSONB DEFAULT '{}',
        breaks_billable     BOOLEAN NOT NULL DEFAULT TRUE,
        lunch_billable      BOOLEAN NOT NULL DEFAULT FALSE,
        lunch_minutes       INTEGER NOT NULL DEFAULT 60,
        active              BOOLEAN NOT NULL DEFAULT TRUE,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_contract_templates_restaurant ON contract_templates(restaurant_id)")

    # ── Staff contract columns ────────────────────────────────────────
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS contract_template_id UUID REFERENCES contract_templates(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS contract_overrides JSONB DEFAULT '{}'")
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS contract_start DATE")

    # ── Overtime requests ─────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS overtime_requests (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        staff_id            UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        restaurant_id       INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
        week_start          DATE NOT NULL,
        regular_minutes     INTEGER NOT NULL DEFAULT 0,
        overtime_minutes    INTEGER NOT NULL DEFAULT 0,
        overtime_rate       NUMERIC(5,2) NOT NULL DEFAULT 1.25,
        status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
        approved_by         UUID REFERENCES staff(id) ON DELETE SET NULL,
        approved_at         TIMESTAMPTZ,
        notes               TEXT DEFAULT '',
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (staff_id, week_start)
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_overtime_requests_restaurant ON overtime_requests(restaurant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_overtime_requests_status ON overtime_requests(status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS overtime_requests CASCADE")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS contract_start")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS contract_overrides")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS contract_template_id")
    op.execute("DROP TABLE IF EXISTS contract_templates CASCADE")
