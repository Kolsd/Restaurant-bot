"""Advanced Staff Management — WebAuthn biometrics, schedules, breaks & payroll.

Creates:
  - webauthn_credentials  (UUID PK) — FIDO2/WebAuthn public keys per staff
  - webauthn_challenges   (UUID PK) — transient ceremony challenges (5-min TTL)
  - staff_schedules       (UUID PK) — expected weekly schedule per employee
  - staff_breaks          (UUID PK) — break log within shifts
  - payroll_runs          (UUID PK) — immutable payroll period snapshots

Alters:
  - staff: adds hourly_rate, photo_url, deductions columns

Key constraints:
  - uq_breaks_one_open: PARTIAL UNIQUE INDEX on staff_breaks(staff_id)
    WHERE break_end IS NULL — mirrors the shift pattern, enforcing one open
    break at a time per employee.
  - payroll_runs UNIQUE(restaurant_id, period_start, period_end) — prevents
    duplicate payroll runs for the same period.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-02
"""
from alembic import op

revision      = "0005"
down_revision = "0004"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── WebAuthn credentials ──────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS webauthn_credentials (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        staff_id        UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        credential_id   TEXT NOT NULL UNIQUE,
        public_key      BYTEA NOT NULL,
        sign_count      INTEGER NOT NULL DEFAULT 0,
        transports      JSONB NOT NULL DEFAULT '[]'::jsonb,
        device_name     TEXT NOT NULL DEFAULT '',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_webauthn_staff ON webauthn_credentials(staff_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_webauthn_cred ON webauthn_credentials(credential_id)")

    # ── WebAuthn challenges (transient, 5-min TTL) ────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS webauthn_challenges (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        challenge       TEXT NOT NULL UNIQUE,
        staff_id        UUID,
        type            TEXT NOT NULL,
        restaurant_id   INTEGER NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)

    # ── Staff schedules ───────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS staff_schedules (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        staff_id        UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        restaurant_id   INTEGER NOT NULL,
        day_of_week     SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time      TIME NOT NULL,
        end_time        TIME NOT NULL,
        active          BOOLEAN NOT NULL DEFAULT TRUE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_schedules_staff ON staff_schedules(staff_id, day_of_week)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_schedules_restaurant ON staff_schedules(restaurant_id)")

    # ── Staff breaks (pauses within a shift) ──────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS staff_breaks (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        shift_id        UUID NOT NULL REFERENCES staff_shifts(id) ON DELETE CASCADE,
        staff_id        UUID NOT NULL,
        break_start     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        break_end       TIMESTAMPTZ,
        notes           TEXT NOT NULL DEFAULT '',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_breaks_one_open
        ON staff_breaks (staff_id)
        WHERE break_end IS NULL
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_breaks_shift ON staff_breaks(shift_id)")

    # ── Payroll runs ──────────────────────────────────────────────
    op.execute("""
    CREATE TABLE IF NOT EXISTS payroll_runs (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        restaurant_id   INTEGER NOT NULL,
        period_start    DATE NOT NULL,
        period_end      DATE NOT NULL,
        status          TEXT NOT NULL DEFAULT 'draft',
        snapshot        JSONB NOT NULL DEFAULT '[]'::jsonb,
        config          JSONB NOT NULL DEFAULT '{}'::jsonb,
        total_gross     NUMERIC(12,2) NOT NULL DEFAULT 0,
        total_net       NUMERIC(12,2) NOT NULL DEFAULT 0,
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        approved_at     TIMESTAMPTZ,
        UNIQUE(restaurant_id, period_start, period_end)
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_payroll_restaurant ON payroll_runs(restaurant_id, period_start DESC)")

    # ── ALTER staff: new columns ──────────────────────────────────
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC(10,2) NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS photo_url TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS deductions JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    # Reverse order: columns first, then child tables, then parent tables
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS deductions")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS photo_url")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS hourly_rate")
    op.execute("DROP TABLE IF EXISTS payroll_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS staff_breaks CASCADE")
    op.execute("DROP TABLE IF EXISTS staff_schedules CASCADE")
    op.execute("DROP TABLE IF EXISTS webauthn_challenges CASCADE")
    op.execute("DROP TABLE IF EXISTS webauthn_credentials CASCADE")
