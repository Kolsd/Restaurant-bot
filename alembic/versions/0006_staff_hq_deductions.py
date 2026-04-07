"""Staff HQ — document_number, manual deduction items, attendance deductions.

Creates:
  - staff_deduction_items  (UUID PK) — structured manual deduction line items per employee
  - attendance_deductions  (UUID PK) — auto-calculated tardiness/early-departure records

Alters:
  - staff: adds document_number column

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-02
"""
from alembic import op

revision      = "0006"
down_revision = "0005"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── document_number on staff ──────────────────────────────────
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS document_number TEXT NOT NULL DEFAULT ''")

    # ── Manual deduction line items ───────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS staff_deduction_items (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        staff_id        UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        restaurant_id   INTEGER NOT NULL,
        category        TEXT NOT NULL DEFAULT 'custom',
        label           TEXT NOT NULL DEFAULT '',
        type            TEXT NOT NULL DEFAULT 'fixed',
        amount          NUMERIC(12,2) NOT NULL DEFAULT 0,
        active          BOOLEAN NOT NULL DEFAULT TRUE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_deduction_items_staff ON staff_deduction_items(staff_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_deduction_items_restaurant ON staff_deduction_items(restaurant_id)")

    # ── Attendance deductions (tardiness / early departure) ───────
    op.execute("""
    CREATE TABLE IF NOT EXISTS attendance_deductions (
        id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        shift_id         UUID NOT NULL REFERENCES staff_shifts(id) ON DELETE CASCADE,
        staff_id         UUID NOT NULL,
        restaurant_id    INTEGER NOT NULL,
        type             TEXT NOT NULL,
        scheduled_time   TIME NOT NULL,
        actual_time      TIMESTAMPTZ NOT NULL,
        minutes_diff     INTEGER NOT NULL DEFAULT 0,
        deduction_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_att_ded_staff ON attendance_deductions(staff_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_att_ded_shift ON attendance_deductions(shift_id)")

    # ── Payroll runs ───────────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS payroll_runs (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        restaurant_id   INTEGER NOT NULL,
        period_start    DATE NOT NULL,
        period_end      DATE NOT NULL,
        status          TEXT NOT NULL DEFAULT 'draft',
        total_gross     NUMERIC(14,2) NOT NULL DEFAULT 0,
        total_net       NUMERIC(14,2) NOT NULL DEFAULT 0,
        entries         JSONB NOT NULL DEFAULT '[]',
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_payroll_runs_restaurant ON payroll_runs(restaurant_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS payroll_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS attendance_deductions CASCADE")
    op.execute("DROP TABLE IF EXISTS staff_deduction_items CASCADE")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS document_number")
