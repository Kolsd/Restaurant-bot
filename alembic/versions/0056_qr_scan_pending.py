"""
0056 — qr_scan_pending: pre-bind QR scans to a phone for race-free table identification.

Background:
  When a customer scans a table QR, the menu page now asks for their WhatsApp
  number BEFORE redirecting to wa.me. The server records a pending claim
  (bot_number, table_id, phone) so the bot can identify the table by exact
  phone match when the message arrives — eliminating the race that existed
  with the previous "most-recent-scan" heuristic, and removing the need for
  the visible [t:tbl-X] marker in the prefilled message.

  Full design in docs/MESA_QR_ARCHITECTURE.md.

Schema:
  - bot_number: which Mesio bot the QR was associated with.
  - org_id / location_id: tenant scoping for RLS.
  - table_id: TEXT to match restaurant_tables.id (which is text).
  - phone: customer's WhatsApp (normalized: digits only, no '+').
  - geo_verified:
      true  → browser shared GPS, customer is within 50m of the location.
      false → browser shared GPS, customer is far away (likely impostor).
      null  → browser denied or doesn't support geolocation.
  - expires_at: 10 minutes default. Customer must send their first
    WhatsApp message within this window for the claim to be consumed.
  - claimed_at: set by the bot when it consumes the claim to open a session.

Indexes:
  - ix_qr_scan_pending_lookup: hot path for the bot's per-message lookup.
    Partial WHERE claimed_at IS NULL keeps the index small (claimed rows
    are dead weight for this query).
  - ix_qr_scan_pending_expiry: used by the scheduler cleanup task to
    delete expired unclaimed rows.

RLS:
  Standard org_isolation policy on org_id. Same pattern as table_orders.

Revision ID: 0056
Revises: 0055
Create Date: 2026-04-28
"""

from alembic import op


revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qr_scan_pending (
            id              BIGSERIAL PRIMARY KEY,
            bot_number      TEXT      NOT NULL,
            org_id          BIGINT    NOT NULL,
            location_id     BIGINT    NULL,
            table_id        TEXT      NOT NULL,
            phone           TEXT      NOT NULL,
            geo_verified    BOOLEAN   NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '10 minutes',
            claimed_at      TIMESTAMPTZ NULL
        );
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_qr_scan_pending_lookup
            ON qr_scan_pending (phone, bot_number)
            WHERE claimed_at IS NULL;
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_qr_scan_pending_expiry
            ON qr_scan_pending (expires_at)
            WHERE claimed_at IS NULL;
        """
    )

    op.execute("ALTER TABLE qr_scan_pending ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE qr_scan_pending FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY org_isolation ON qr_scan_pending
            USING (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
            WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint);
        """
    )
    # Grant DML to the runtime role (the app connects as mesio_app, not postgres).
    # Without this, every INSERT/SELECT/UPDATE through tenant_connection() raises
    # InsufficientPrivilegeError. Pattern matches 0049_loyalty_campaigns.py.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON qr_scan_pending TO mesio_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE qr_scan_pending_id_seq TO mesio_app")
    # Cross-tenant grants for mesio_superadmin: the bot's lookup
    # (qr_claims_repo.find_unclaimed_by_phone) runs under bypass_tenant_scope
    # to find which org owns a scan claim — by definition the row's org_id is
    # NOT yet known at lookup time, so RLS must be bypassed. The SET LOCAL
    # ROLE mesio_superadmin in tenant_connection() needs explicit grants
    # because mesio_superadmin is NOINHERIT (NOT a superuser shortcut).
    op.execute("GRANT SELECT, UPDATE ON qr_scan_pending TO mesio_superadmin")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qr_scan_pending CASCADE;")
