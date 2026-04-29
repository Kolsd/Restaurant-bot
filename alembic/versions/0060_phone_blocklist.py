"""
0060 — phone_blocklist: Capa 3 anti-impostor phone blocking.

Background:
  When a waiter marks a table as a "ghost" (no real customer present), the
  phone numbers that had active sessions on that table are blocked for 24 h.
  Blocked phones receive a polite rejection message and are not processed
  by the bot.

  org_id is nullable to allow future global blocks (platform-level abuse),
  but in practice every insert from the restaurant flow comes with org_id.
  The RLS policy permits reads where org_id IS NULL (cross-org check, which
  is needed by the inbox_worker using bypass_tenant_scope) or org_id matches
  the current session scope.

  The partial unique index ux_phone_blocklist_active enforces that only ONE
  active block per phone exists at a time.  The unique predicate is
  `blocked_until > NOW()` — expired rows do not participate, so a phone
  can be re-blocked after expiry without a conflict.

RLS: org_isolation policy (same pattern as qr_scan_pending).

Revision ID: 0060
Revises: 0059
Create Date: 2026-04-29
"""

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS phone_blocklist (
            id              BIGSERIAL PRIMARY KEY,
            phone           TEXT NOT NULL,
            blocked_until   TIMESTAMPTZ NOT NULL,
            reason          TEXT NOT NULL,
            org_id          BIGINT NULL,
            blocked_by      TEXT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    # Partial unique index: only ONE active block per phone.
    # Expired rows (blocked_until <= NOW()) do not count toward uniqueness.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_phone_blocklist_active
            ON phone_blocklist (phone)
            WHERE blocked_until > NOW();
        """
    )
    op.execute("ALTER TABLE phone_blocklist ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE phone_blocklist FORCE ROW LEVEL SECURITY;")
    # Policy allows:
    #   - rows with org_id IS NULL (global/platform blocks, readable by all)
    #   - rows where org_id matches the current session scope
    # The inbox_worker check (is_phone_blocked) uses bypass_tenant_scope so
    # it can see ALL active blocks regardless of org.
    op.execute(
        """
        CREATE POLICY org_isolation ON phone_blocklist
            USING (
                org_id IS NULL
                OR org_id = NULLIF(current_setting('app.org_id', true), '')::bigint
            )
            WITH CHECK (
                org_id IS NULL
                OR org_id = NULLIF(current_setting('app.org_id', true), '')::bigint
            );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS phone_blocklist;")
