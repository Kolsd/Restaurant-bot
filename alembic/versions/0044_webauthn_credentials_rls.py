"""webauthn_credentials: add org_id + RLS org_isolation policy.

GAP CLOSED:
  webauthn_credentials had no org_id column and was not in any RLS table list.
  credential_id is globally UNIQUE, which means a cross-tenant auth ceremony
  could succeed if an attacker guessed a credential_id from another tenant.
  This migration adds the org_id FK, backfills from staff.org_id, then enables
  the same org_isolation RLS policy used by every other post-Wave-2 table.

BACKFILL:
  All existing credentials are linked to a staff member. staff.org_id is
  already NOT NULL (set by Wave-2 backfill in 0035). The UPDATE is safe for
  any DB size — it's a single pass joining on the PK.

DOWNGRADE WARNING:
  Dropping org_id is non-destructive (data not lost, just the column).
  RLS is disabled before the column drop so that the table is accessible again.

Revision ID: 0044_webauthn_cred_rls
Revises: 0043_shift_swap
Create Date: 2026-04-20
"""

import logging

from alembic import op

revision = "0044_webauthn_cred_rls"
down_revision = "0043_shift_swap"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_POLICY_NAME = "org_isolation"
_ORG_ISOLATION_EXPR = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)


def upgrade() -> None:
    logger.info("0044 upgrade: adding org_id to webauthn_credentials")

    # 1. Add column nullable first (safe — no lock escalation on empty default)
    op.execute("""
        ALTER TABLE webauthn_credentials
        ADD COLUMN IF NOT EXISTS org_id BIGINT NULL
    """)

    # 2. Backfill from staff.org_id via staff_id FK
    op.execute("""
        UPDATE webauthn_credentials wc
        SET org_id = s.org_id
        FROM staff s
        WHERE wc.staff_id = s.id
          AND wc.org_id IS NULL
    """)

    # Log any orphaned rows (staff deleted, no match) — should be 0
    op.execute("""
        DO $$
        DECLARE orphan_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO orphan_count
            FROM webauthn_credentials
            WHERE org_id IS NULL;
            IF orphan_count > 0 THEN
                RAISE WARNING '0044: % webauthn_credentials rows have NULL org_id after backfill — orphaned staff FK?', orphan_count;
            END IF;
        END $$;
    """)

    # 3. Set NOT NULL constraint
    op.execute("""
        ALTER TABLE webauthn_credentials
        ALTER COLUMN org_id SET NOT NULL
    """)

    # 4. Add FK to organizations
    op.execute("""
        ALTER TABLE webauthn_credentials
        ADD CONSTRAINT fk_webauthn_cred_org
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
    """)

    # 5. Index for RLS policy scans
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_webauthn_credentials_org_id
        ON webauthn_credentials (org_id)
    """)

    # 6. Enable + Force RLS
    op.execute("ALTER TABLE webauthn_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webauthn_credentials FORCE ROW LEVEL SECURITY")

    # 7. Create org_isolation policy (idempotent)
    op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON webauthn_credentials")
    op.execute(f"""
        CREATE POLICY {_POLICY_NAME} ON webauthn_credentials
        USING ({_ORG_ISOLATION_EXPR})
        WITH CHECK ({_ORG_ISOLATION_EXPR})
    """)

    logger.info("0044 upgrade: webauthn_credentials RLS active")


def downgrade() -> None:
    logger.info("0044 downgrade: removing RLS + org_id from webauthn_credentials")

    op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON webauthn_credentials")
    op.execute("ALTER TABLE webauthn_credentials DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webauthn_credentials NO FORCE ROW LEVEL SECURITY")

    op.execute("DROP INDEX IF EXISTS ix_webauthn_credentials_org_id")

    op.execute("""
        ALTER TABLE webauthn_credentials
        DROP CONSTRAINT IF EXISTS fk_webauthn_cred_org
    """)

    op.execute("""
        ALTER TABLE webauthn_credentials
        DROP COLUMN IF EXISTS org_id
    """)

    logger.info("0044 downgrade: done")
