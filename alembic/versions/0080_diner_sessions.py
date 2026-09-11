"""Add diner_sessions table — anonymous web-chat identity for the Mesio chat channel.

Background (Mesio-native chat pivot, see docs/PRODUCT_CONTEXT.md):
  The QR now opens a Mesio chat page instead of WhatsApp. A diner scans,
  browses, and orders with ZERO friction — no phone, no form. Mesio mints a
  synthetic identity token ("web:<uuid4>") the same way a real WhatsApp
  number flows through `conversations`, `carts`, `state_store` keys, and NPS
  today (phone is an OPAQUE identity string everywhere in this codebase, not
  a validated number). This table is the durable record binding that token
  to (org, location, table | order_mode) plus an OPTIONAL phone/name
  captured later at payment time.

Schema:
  - token:        the diner's identity (`web:<uuid4>`), used as `phone` in
                   every existing phone-keyed function (agent.chat(),
                   carts, NPS, waiter_alerts, ...).
  - org_id:       tenant key (RLS scope).
  - location_id:  which sede — nullable (resolved lazily like the bot's
                   normal `location_id`, e.g. before a delivery/pickup GPS
                   fix arrives).
  - table_id:     restaurant_tables.id (TEXT) for dine-in QR entries. NULL
                   for delivery/pickup web-chat entries.
  - table_name:   denormalized so /api/diner/waiter-call doesn't need an
                   extra lookup on every call.
  - bot_number:   the tenant's WhatsApp/bot number — agent.chat() requires
                   it as the routing key exactly like the WhatsApp channel.
  - order_mode:   'dine_in' | 'delivery' | 'pickup'.
  - phone / display_name: OPTIONAL, captured at payment time (PM decision:
                   never required to browse or chat).

RLS: standard org_isolation policy (ENABLE + FORCE), matching every other
tenant-scoped table in the RLS surface (see CLAUDE.md "Blindaje Multi-tenant
RLS"). mesio_superadmin additionally gets an explicit SELECT/UPDATE grant
(belt-and-suspenders alongside the 0079 default-privileges mechanism) because
the by-token lookup in diner_sessions_repo.get_by_token() runs UNDER
bypass_tenant_scope — a request only carries the token, not the org_id, so
the tenant cannot be known yet (same pattern as qr_scan_pending, see 0056).

Revision ID: 0080_diner_sessions
Revises:     0079_bootstrap_role_grants
Create Date: 2026-09-10
"""

from alembic import op

revision = "0080_diner_sessions"
down_revision = "0079_bootstrap_role_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS diner_sessions (
            id             BIGSERIAL PRIMARY KEY,
            token          TEXT        NOT NULL,
            org_id         BIGINT      NOT NULL,
            location_id    BIGINT      NULL,
            table_id       TEXT        NULL,
            table_name     TEXT        NULL,
            bot_number     TEXT        NOT NULL,
            order_mode     TEXT        NOT NULL DEFAULT 'dine_in',
            phone          TEXT        NULL,
            display_name   TEXT        NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_diner_sessions_token
            ON diner_sessions (token);
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_diner_sessions_org_created
            ON diner_sessions (org_id, created_at);
        """
    )

    op.execute("ALTER TABLE diner_sessions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE diner_sessions FORCE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS org_isolation ON diner_sessions;")
    op.execute(
        """
        CREATE POLICY org_isolation ON diner_sessions
            USING (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
            WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint);
        """
    )

    # Belt-and-suspenders grants (0079 already covers new tables via
    # ALTER DEFAULT PRIVILEGES, but we make it explicit here so this
    # migration is self-contained and safe to reason about in isolation —
    # same rationale as 0056_qr_scan_pending.py).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON diner_sessions TO mesio_app;")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE diner_sessions_id_seq TO mesio_app;")
    op.execute("GRANT SELECT, UPDATE ON diner_sessions TO mesio_superadmin;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS diner_sessions CASCADE;")
