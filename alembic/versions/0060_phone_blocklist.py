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

  Index strategy: a regular (non-partial, non-unique) index on phone for
  the lookup hot path (is_phone_blocked filters by `phone = $1 AND
  blocked_until > NOW()`). A partial unique index `WHERE blocked_until >
  NOW()` was the original design, but Postgres rejects predicates that
  reference NOW() — the function is STABLE, not IMMUTABLE, and partial
  index predicates require IMMUTABLE expressions. The "one active block
  per phone" semantic is enforced in app code by `add_to_blocklist` doing
  ON CONFLICT against the (phone) column with active-window check
  inside the upsert logic. cleanup_expired() prunes rows older than the
  expiry window so the table doesn't grow unbounded.

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
    # Lookup index: hot path is `WHERE phone = $1 AND blocked_until > NOW()`.
    # Postgres rejects partial indexes whose predicate references NOW() (must
    # be IMMUTABLE). Plain (non-unique) index covers the lookup; the "one
    # active block per phone" semantic is handled in app code (see
    # add_to_blocklist in phone_blocklist_repo.py — uses an UPDATE-then-INSERT
    # pattern to dedupe). cleanup_expired() drops stale rows.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_phone_blocklist_phone
            ON phone_blocklist (phone, blocked_until DESC);
        """
    )
    # Grant runtime role access to the new table. Without these the app
    # connection (mesio_app) hits permission denied as soon as it tries
    # to read/write phone_blocklist. Mirrors the grants applied to other
    # tables in earlier migrations / conftest setup.
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mesio_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON phone_blocklist TO mesio_app';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE phone_blocklist_id_seq TO mesio_app';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mesio_superadmin') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON phone_blocklist TO mesio_superadmin';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE phone_blocklist_id_seq TO mesio_superadmin';
            END IF;
        END $$;
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
