"""orders.nps_answered_at — one rating (or skip) per delivery/pickup order.

The status page deduplicated ratings with state_store.nps_is_done(phone,
bot_number). On the web channel `phone` is the browser's session token,
which the ordering page keeps across orders — so a returning customer could
rate their first order and never any later one. The flag also lived in
Redis with a TTL (in-process without Redis, i.e. per worker), so it was
neither durable nor shared.

A nullable timestamp on the order records that the customer answered the
survey (rated or skipped). It is claimed with a conditional UPDATE, so two
concurrent submits cannot both pass.

Revision ID: 0087_orders_nps_answered_at
Revises:     0086_staff_location_backfill
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0087_orders_nps_answered_at"
down_revision = "0086_staff_location_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS nps_answered_at TIMESTAMPTZ NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE orders DROP COLUMN IF EXISTS nps_answered_at"))
