"""WhatsApp leaves the schema: the bot key, the numbers and the Meta tokens.

PM decision 2026-09-29 (cleanup 5b): WhatsApp goes "de todo lado" so the
new messaging system is not built on top of it. Since 0098 the code keys on
`org_id`; nothing reads these columns any more:

  * `bot_number` on the 12 bot tables (and its indexes, and the transition
    unique indexes 0098 left on (phone, bot_number));
  * `whatsapp_number`, `wa_phone_id`, `wa_access_token` on organizations and
    locations — the restaurants' WhatsApp numbers and their Meta access
    tokens. Deleting the tokens is the point: a credential nobody uses is
    only a liability. IRREVERSIBLE: the downgrade restores the columns
    empty;
  * `table_sessions.meta_phone_id`, `crm_templates.wa_name`,
    `prospect_interactions.wa_message_id`.

The `restaurants` view is recreated without those columns.

Also: `reservations` never had row-level security (every other tenant table
has had it since 0036). It is enabled and forced here with the same
`org_isolation` policy.

Revision ID: 0099_drop_whatsapp_columns
Revises:     0098_bot_key_to_org_id
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0099_drop_whatsapp_columns"
down_revision = "0098_bot_key_to_org_id"
branch_labels = None
depends_on = None

_BOT_TABLES = (
    "carts", "conversations", "diner_sessions", "menu_events",
    "nps_responses", "nps_waiting", "orders", "qr_scan_pending",
    "reservations", "table_orders", "table_sessions", "waiter_alerts",
)
_KEYED = ("carts", "conversations", "nps_waiting")
_WA_COLUMNS = ("whatsapp_number", "wa_phone_id", "wa_access_token")

_DISPLAY_NAME = """
        CASE
            WHEN (SELECT count(*) FROM locations l2 WHERE l2.org_id = o.id) > 1
                 AND l.name IS NOT NULL
                 AND btrim(l.name) <> ''
                 AND lower(btrim(l.name)) IS DISTINCT FROM lower(btrim(COALESCE(o.name, '')))
            THEN COALESCE(o.name, l.name) || ' · ' || l.name
            ELSE COALESCE(o.name, l.name)
        END AS display_name
"""

_VIEW = f"""
    CREATE VIEW restaurants AS
    SELECT l.id,
        COALESCE(o.name, l.name) AS name,
        l.name AS location_name,
        l.address,
        l.latitude,
        l.longitude,
        o.menu,
        o.features,
        o.slug,
        o.billing_config,
        o.subscription_status,
        o.subscription_plan,
        l.created_at,
        l.updated_at,
        {_DISPLAY_NAME}
    FROM locations l
    JOIN organizations o ON o.id = l.org_id
"""

# 0096's view, for the downgrade.
_OLD_VIEW = f"""
    CREATE VIEW restaurants AS
    SELECT l.id,
        COALESCE(o.name, l.name) AS name,
        l.name AS location_name,
        COALESCE(l.whatsapp_number, o.whatsapp_number, 'web' || o.id::text) AS whatsapp_number,
        l.address,
        l.latitude,
        l.longitude,
        o.menu,
        o.features,
        COALESCE(l.wa_phone_id, o.wa_phone_id) AS wa_phone_id,
        COALESCE(l.wa_access_token, o.wa_access_token) AS wa_access_token,
        o.slug,
        o.billing_config,
        o.subscription_status,
        o.subscription_plan,
        l.created_at,
        l.updated_at,
        {_DISPLAY_NAME}
    FROM locations l
    JOIN organizations o ON o.id = l.org_id
"""

_POLICY = "(org_id = (NULLIF(current_setting('app.org_id', true), ''))::bigint)"

# Recreating the view drops its grants; restore what the app roles had.
_GRANTS = """
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['mesio_app', 'mesio_superadmin'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON restaurants TO %I', r);
        END IF;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS restaurants"))
    for t in _KEYED:
        op.execute(sa.text(f"DROP INDEX IF EXISTS ux_{t}_phone_bot"))
    for t in _BOT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {t} DROP COLUMN IF EXISTS bot_number"))
    for t in ("organizations", "locations"):
        for c in _WA_COLUMNS:
            op.execute(sa.text(f"ALTER TABLE {t} DROP COLUMN IF EXISTS {c}"))
    op.execute(sa.text("ALTER TABLE table_sessions DROP COLUMN IF EXISTS meta_phone_id"))
    op.execute(sa.text("ALTER TABLE crm_templates DROP COLUMN IF EXISTS wa_name"))
    op.execute(sa.text("ALTER TABLE prospect_interactions DROP COLUMN IF EXISTS wa_message_id"))
    op.execute(sa.text(_VIEW))
    op.execute(sa.text(_GRANTS))

    op.execute(sa.text("ALTER TABLE reservations ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE reservations FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS org_isolation ON reservations"))
    op.execute(sa.text(
        f"CREATE POLICY org_isolation ON reservations USING {_POLICY} WITH CHECK {_POLICY}"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS org_isolation ON reservations"))
    op.execute(sa.text("ALTER TABLE reservations NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE reservations DISABLE ROW LEVEL SECURITY"))

    op.execute(sa.text("DROP VIEW IF EXISTS restaurants"))
    op.execute(sa.text("ALTER TABLE prospect_interactions ADD COLUMN IF NOT EXISTS wa_message_id TEXT"))
    op.execute(sa.text("ALTER TABLE crm_templates ADD COLUMN IF NOT EXISTS wa_name TEXT"))
    op.execute(sa.text("ALTER TABLE table_sessions ADD COLUMN IF NOT EXISTS meta_phone_id TEXT"))
    for t in ("organizations", "locations"):
        for c in _WA_COLUMNS:
            op.execute(sa.text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} TEXT"))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_organizations_whatsapp "
        "ON organizations (whatsapp_number) WHERE whatsapp_number IS NOT NULL"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_locations_whatsapp "
        "ON locations (whatsapp_number) WHERE whatsapp_number IS NOT NULL"
    ))
    for t in _BOT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS bot_number TEXT"))
        op.execute(sa.text(f"UPDATE {t} SET bot_number = 'web' || org_id::text"))
    for t in _KEYED:
        op.execute(sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{t}_phone_bot ON {t} (phone, bot_number)"
        ))
    op.execute(sa.text(_OLD_VIEW))
    op.execute(sa.text(_GRANTS))
