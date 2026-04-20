"""Add channel + waiter/staff attribution columns to orders, table_orders, table_sessions.

Purely additive. All columns are NULL-able — legacy rows stay NULL.
Idempotent via IF NOT EXISTS guards on every ADD COLUMN and CREATE INDEX.

New columns
-----------
orders.channel TEXT NULL
    Values: whatsapp_bot | pos | qr_pickup | web | manual
    Partial index on (channel) WHERE channel IS NOT NULL (analytics queries).

table_orders.channel TEXT NULL
    Values: pos | qr_table | whatsapp_bot | manual
table_orders.waiter_staff_id INT NULL → staff(id) ON DELETE SET NULL

table_sessions.assigned_staff_id INT NULL → staff(id) ON DELETE SET NULL

Downgrade drops indexes then columns.

Revision ID: 0039_channel_attr
Revises: 0038_drop_legacy_compat
Create Date: 2026-04-19
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0039_channel_attr"
down_revision = "0038_drop_legacy_compat"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    # ── orders ────────────────────────────────────────────────────────────────
    logger.info("0039: adding orders.channel")
    op.execute(sa.text(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS channel TEXT NULL"
    ))
    logger.info("0039: creating ix_orders_channel")
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_orders_channel "
        "ON orders (channel) WHERE channel IS NOT NULL"
    ))

    # ── table_orders ──────────────────────────────────────────────────────────
    logger.info("0039: adding table_orders.channel")
    op.execute(sa.text(
        "ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS channel TEXT NULL"
    ))
    logger.info("0039: adding table_orders.waiter_staff_id")
    op.execute(sa.text(
        "ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS "
        "waiter_staff_id UUID NULL REFERENCES staff(id) ON DELETE SET NULL"
    ))
    logger.info("0039: creating ix_table_orders_channel")
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_table_orders_channel "
        "ON table_orders (channel) WHERE channel IS NOT NULL"
    ))
    logger.info("0039: creating ix_table_orders_waiter")
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_table_orders_waiter "
        "ON table_orders (waiter_staff_id) WHERE waiter_staff_id IS NOT NULL"
    ))

    # ── table_sessions ────────────────────────────────────────────────────────
    logger.info("0039: adding table_sessions.assigned_staff_id")
    op.execute(sa.text(
        "ALTER TABLE table_sessions ADD COLUMN IF NOT EXISTS "
        "assigned_staff_id UUID NULL REFERENCES staff(id) ON DELETE SET NULL"
    ))
    logger.info("0039: creating ix_table_sessions_assigned_staff")
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_table_sessions_assigned_staff "
        "ON table_sessions (assigned_staff_id) WHERE assigned_staff_id IS NOT NULL"
    ))

    logger.info("0039: complete — channel + attribution columns added")


def downgrade() -> None:
    # Drop indexes first, then columns.
    logger.info("0039 downgrade: dropping indexes")
    op.execute(sa.text("DROP INDEX IF EXISTS ix_table_sessions_assigned_staff"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_table_orders_waiter"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_table_orders_channel"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_orders_channel"))

    logger.info("0039 downgrade: dropping columns")
    op.execute(sa.text(
        "ALTER TABLE table_sessions DROP COLUMN IF EXISTS assigned_staff_id"
    ))
    op.execute(sa.text(
        "ALTER TABLE table_orders DROP COLUMN IF EXISTS waiter_staff_id"
    ))
    op.execute(sa.text(
        "ALTER TABLE table_orders DROP COLUMN IF EXISTS channel"
    ))
    op.execute(sa.text(
        "ALTER TABLE orders DROP COLUMN IF EXISTS channel"
    ))
    logger.info("0039 downgrade: complete")
