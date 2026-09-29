"""The bot tenant key moves from `bot_number` to `org_id` — transition step.

`bot_number` was the WhatsApp number the bot answered on (or `web<org_id>`
for an org without one, 0096): ONE key per organization, shared by its
sedes. Every table that carries it already has `org_id NOT NULL`, which is
the same key without the WhatsApp detour, so the code now keys on `org_id`
(and tells sedes apart by `location_id`, as it already did).

This revision only makes the move possible without a flag day:
  * the three tables whose primary key was `(phone, bot_number)` get a
    unique `(phone, org_id)` instead (a sede with its own WhatsApp number
    could leave two rows per org — the newest one wins; they are carts,
    conversation state and pending NPS, all short-lived);
  * every `bot_number` index gets its `org_id` twin;
  * `bot_number` becomes nullable, so code that no longer writes it works.

The `bot_number` columns and the WhatsApp columns are dropped in the next
revision, once no code reads them.

Revision ID: 0098_bot_key_to_org_id
Revises:     0097_table_orders_location
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0098_bot_key_to_org_id"
down_revision = "0097_table_orders_location"
branch_labels = None
depends_on = None

_KEYED = ("carts", "conversations", "nps_waiting")

_BOT_TABLES = (
    "carts", "conversations", "diner_sessions", "menu_events",
    "nps_responses", "nps_waiting", "orders", "qr_scan_pending",
    "reservations", "table_orders", "table_sessions", "waiter_alerts",
)

_NEW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_conversations_org_updated ON conversations (org_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_nps_responses_org_created ON nps_responses (org_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_orders_org_created ON orders (org_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_qr_scan_pending_org_lookup ON qr_scan_pending (phone, org_id) WHERE claimed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_reservations_org_date ON reservations (org_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_table_sessions_org_active ON table_sessions (phone, org_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_table_sessions_org_closed ON table_sessions (org_id, closed_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_waiter_alerts_org ON waiter_alerts (org_id, dismissed, created_at DESC)",
)


def upgrade() -> None:
    for t in _KEYED:
        # Keep the newest row per (phone, org_id); ctid breaks exact ties.
        op.execute(sa.text(f"""
            DELETE FROM {t} a
             USING {t} b
             WHERE a.phone = b.phone
               AND a.org_id = b.org_id
               AND (a.{_ts(t)}, a.ctid) < (b.{_ts(t)}, b.ctid)
        """))
        op.execute(sa.text(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {t}_pkey"))
        op.execute(sa.text(f"ALTER TABLE {t} ADD CONSTRAINT {t}_pkey PRIMARY KEY (phone, org_id)"))
        # Until the code stops upserting ON CONFLICT (phone, bot_number).
        op.execute(sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{t}_phone_bot ON {t} (phone, bot_number)"
        ))
    for t in _BOT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {t} ALTER COLUMN bot_number DROP NOT NULL"))
    for ddl in _NEW_INDEXES:
        op.execute(sa.text(ddl))


def _ts(table: str) -> str:
    return "created_at" if table == "nps_waiting" else "updated_at"


def downgrade() -> None:
    for ddl in _NEW_INDEXES:
        name = ddl.split(" IF NOT EXISTS ")[1].split(" ")[0]
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    for t in _BOT_TABLES:
        op.execute(sa.text(
            f"UPDATE {t} SET bot_number = 'web' || org_id::text WHERE bot_number IS NULL"
        ))
    for t in _BOT_TABLES:
        if t in ("menu_events", "table_orders"):
            continue  # nullable before this revision
        op.execute(sa.text(f"ALTER TABLE {t} ALTER COLUMN bot_number SET NOT NULL"))
    for t in _KEYED:
        op.execute(sa.text(f"DROP INDEX IF EXISTS ux_{t}_phone_bot"))
        op.execute(sa.text(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {t}_pkey"))
        op.execute(sa.text(f"ALTER TABLE {t} ADD CONSTRAINT {t}_pkey PRIMARY KEY (phone, bot_number)"))
