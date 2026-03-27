"""Add roles JSONB to staff table for multi-role support.

Unifies Mi Equipo + Staff roster: staff members are now the single
source of truth for all restaurant employees. The roles JSONB array
replaces the single TEXT role column for multi-role assignments.

Migration is safe on existing databases:
  - ADD COLUMN IF NOT EXISTS with a default value
  - UPDATE backfills roles from the existing role column

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-27
"""
from alembic import op

revision      = "0004"
down_revision = "0003"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # Add roles JSONB array; default empty array for safety
    op.execute("""
        ALTER TABLE staff
            ADD COLUMN IF NOT EXISTS roles JSONB NOT NULL DEFAULT '[]'::jsonb
    """)

    # Backfill: populate roles from existing single-role column so existing
    # employees don't lose their role assignment after the migration.
    op.execute("""
        UPDATE staff
           SET roles = jsonb_build_array(role)
         WHERE roles = '[]'::jsonb
           AND role IS NOT NULL
           AND role <> ''
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_roles ON staff USING gin(roles)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_staff_roles")
    op.execute("ALTER TABLE staff DROP COLUMN IF EXISTS roles")
