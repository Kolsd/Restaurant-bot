"""Policy naming consistency: rename tenant_isolation_org → org_isolation.

GAP CLOSED:
  Migrations 0042 (staff_announcements, staff_tasks, staff_task_completions)
  and 0043 (shift_swap_requests) created RLS policies named 'tenant_isolation_org'.
  All other post-Wave-2 tables use the canonical name 'org_isolation'. The
  inconsistency means automated audits (grep, pg_policies queries) miss these
  four tables when checking for the canonical policy name.

  This migration renames all four to 'org_isolation' — no functional change,
  policy expression is identical.

Revision ID: 0045_policy_naming
Revises: 0044_webauthn_cred_rls
Create Date: 2026-04-20
"""

import logging

from alembic import op

revision = "0045_policy_naming"
down_revision = "0044_webauthn_cred_rls"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Tables whose policy needs renaming
_TABLES = [
    "staff_announcements",
    "staff_tasks",
    "staff_task_completions",
    "shift_swap_requests",
]


def upgrade() -> None:
    for table in _TABLES:
        logger.info("0045 upgrade: renaming policy on %s", table)
        op.execute(f"""
            ALTER POLICY tenant_isolation_org ON {table}
            RENAME TO org_isolation
        """)
    logger.info("0045 upgrade: all policies renamed to org_isolation")


def downgrade() -> None:
    for table in reversed(_TABLES):
        logger.info("0045 downgrade: reverting policy name on %s", table)
        op.execute(f"""
            ALTER POLICY org_isolation ON {table}
            RENAME TO tenant_isolation_org
        """)
    logger.info("0045 downgrade: all policies reverted to tenant_isolation_org")
