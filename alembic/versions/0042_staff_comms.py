"""Staff communications: announcements + task checklist.

Adds three tables for admin→staff communication:
  - staff_announcements  : one-way broadcast messages from admin/gerente.
  - staff_tasks          : checklist items assigned to the whole org.
  - staff_task_completions: per-staff completion tracking (one row per
                             staff per task, ON CONFLICT DO UPDATE).

All three tables carry org_id NOT NULL (FK → organizations) and are
protected by org_isolation RLS policy matching the pattern established
in migrations 0036 / 0037.

Revision ID: 0042_staff_comms
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0042_staff_comms"
down_revision = "0041_drop_legacy_rid"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# The org_isolation predicate used by every new RLS table.
# ---------------------------------------------------------------------------
_ORG_ISOLATION_EXPR = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)


def upgrade() -> None:
    logger.info("0042 upgrade: creating staff_announcements table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff_announcements (
            id           BIGSERIAL PRIMARY KEY,
            org_id       BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            title        TEXT NOT NULL,
            body         TEXT NOT NULL DEFAULT '',
            created_by   TEXT REFERENCES users(username) ON DELETE SET NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            published_at TIMESTAMPTZ NULL,
            expires_at   TIMESTAMPTZ NULL,
            active       BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    logger.info("0042 upgrade: creating staff_tasks table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff_tasks (
            id          BIGSERIAL PRIMARY KEY,
            org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_by  TEXT REFERENCES users(username) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            due_date    DATE NULL,
            active      BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    logger.info("0042 upgrade: creating staff_task_completions table")
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff_task_completions (
            id           BIGSERIAL PRIMARY KEY,
            task_id      BIGINT NOT NULL REFERENCES staff_tasks(id) ON DELETE CASCADE,
            staff_id     UUID NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            org_id       BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            notes        TEXT NULL,
            CONSTRAINT uq_staff_task_completion UNIQUE (task_id, staff_id)
        )
    """)

    # ── Indexes ──────────────────────────────────────────────────────────────
    logger.info("0042 upgrade: creating indexes")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_staff_announcements_org_active
        ON staff_announcements (org_id, active, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_staff_tasks_org_active
        ON staff_tasks (org_id, active, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_staff_task_completions_staff
        ON staff_task_completions (staff_id, completed_at DESC)
    """)

    # ── RLS ──────────────────────────────────────────────────────────────────
    logger.info("0042 upgrade: enabling RLS on new tables")
    for table in ("staff_announcements", "staff_tasks", "staff_task_completions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation_org ON {table}
            USING ({_ORG_ISOLATION_EXPR})
            WITH CHECK ({_ORG_ISOLATION_EXPR})
        """)

    logger.info("0042 upgrade: done")


def downgrade() -> None:
    logger.info("0042 downgrade: dropping staff comms tables")
    for table in ("staff_task_completions", "staff_tasks", "staff_announcements"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    logger.info("0042 downgrade: done")
