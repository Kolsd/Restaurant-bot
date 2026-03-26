"""Phase 6 — Staff, Shifts & Tips module.

Creates:
  - staff             (UUID PK) — employee roster per restaurant
  - staff_shifts      (UUID PK) — clock-in / clock-out log
  - tip_distributions (UUID PK) — tip-cut history with dynamic split config

Alters:
  - table_checks: adds tip_amount NUMERIC(10,2) DEFAULT 0

Key constraints:
  - uq_staff_shifts_one_open: PARTIAL UNIQUE INDEX on staff_shifts(staff_id)
    WHERE clock_out IS NULL — enforces at DB level that one employee can only
    have ONE open shift at a time. When clock_out is set the row leaves the
    index, allowing a new open shift later without dropping the constraint.

UUID primary keys:
  All new tables use gen_random_uuid() (native in PostgreSQL 13+, no extension
  needed on Railway). The browser can generate matching UUIDs via
  crypto.randomUUID() for offline-first sync via POST /api/sync.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-25
"""
from alembic import op

revision      = "0002"
down_revision = "0001"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── 1. staff — employee roster ───────────────────────────────────────────
    # role values (open enum, stored as text):
    #   mesero | cocina | bar | caja | gerente | otro
    # pin stores the bcrypt hash of the employee's 4-digit PIN.
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            name          TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'mesero',
            pin           TEXT    NOT NULL DEFAULT '',
            active        BOOLEAN NOT NULL DEFAULT TRUE,
            phone         TEXT    NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_restaurant "
        "ON staff(restaurant_id) WHERE active = TRUE"
    )

    # ── 2. staff_shifts — clock-in / clock-out log ───────────────────────────
    # clock_out IS NULL  →  shift is currently OPEN.
    # clock_out NOT NULL →  shift is closed; employee has clocked out.
    #
    # restaurant_id is denormalized here (also accessible via staff FK) so that
    # queries like "all open shifts for restaurant X" avoid an extra JOIN.
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff_shifts (
            id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
            staff_id      UUID    NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            restaurant_id INTEGER NOT NULL,
            clock_in      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            clock_out     TIMESTAMPTZ,
            notes         TEXT    NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── PARTIAL UNIQUE INDEX — the critical business rule ────────────────────
    # Guarantees at the database level: each employee can have AT MOST ONE row
    # where clock_out IS NULL (i.e., one open shift).
    #
    # When clock_out is set to a timestamp, the row leaves the partial index,
    # so the same staff_id can open new shifts in the future without violating
    # the constraint. No trigger needed — the DBMS enforces this automatically.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_shifts_one_open
            ON staff_shifts (staff_id)
            WHERE clock_out IS NULL
    """)

    # Supporting indexes
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_shifts_restaurant "
        "ON staff_shifts(restaurant_id, clock_in DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_shifts_staff "
        "ON staff_shifts(staff_id, clock_in DESC)"
    )

    # ── 3. tip_distributions — corte de propinas ─────────────────────────────
    # distribution: JSON array — one entry per employee who participated:
    #   [{"staff_id": "uuid", "name": "Ana", "role": "mesero",
    #     "amount": 25000, "pct": 50}]
    #
    # pct_config: snapshot of restaurants.features->'tip_distribution' at the
    # time of the cut — preserves the config even if the owner changes it later.
    #   {"mesero": 50, "cocina": 30, "bar": 20}
    op.execute("""
        CREATE TABLE IF NOT EXISTS tip_distributions (
            id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
            restaurant_id  INTEGER NOT NULL,
            period_start   TIMESTAMPTZ NOT NULL,
            period_end     TIMESTAMPTZ NOT NULL,
            total_tips     NUMERIC(12,2) NOT NULL DEFAULT 0,
            distribution   JSONB   NOT NULL DEFAULT '[]'::jsonb,
            pct_config     JSONB   NOT NULL DEFAULT '{}'::jsonb,
            created_by     TEXT    NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tip_dist_restaurant "
        "ON tip_distributions(restaurant_id, created_at DESC)"
    )

    # ── 4. table_checks — add tip_amount column ──────────────────────────────
    # Stores the tip collected at the time of check payment.
    # DEFAULT 0 means existing rows (all currently paid checks) have tip = 0,
    # which is the correct semantic (retroactive tips are not meaningful).
    op.execute("""
        ALTER TABLE table_checks
            ADD COLUMN IF NOT EXISTS tip_amount NUMERIC(10,2) NOT NULL DEFAULT 0
    """)


def downgrade() -> None:
    # Reverse in strict dependency order (child tables before parents).

    # 4. Remove tip_amount from table_checks
    op.execute("ALTER TABLE table_checks DROP COLUMN IF EXISTS tip_amount")

    # 3. Drop tip_distributions
    op.execute("DROP TABLE IF EXISTS tip_distributions")

    # 2. Drop staff_shifts (CASCADE drops uq_staff_shifts_one_open and all indexes)
    op.execute("DROP TABLE IF EXISTS staff_shifts CASCADE")

    # 1. Drop staff (CASCADE drops idx_staff_restaurant)
    op.execute("DROP TABLE IF EXISTS staff CASCADE")
