"""Add orders.customer_email — optional email captured at web delivery/pickup
checkout (docs/claude/delivery-web.md, chunk 3).

Why a NEW migration instead of folding this into 0082_delivery_web: chunk-3
instructions are explicit that 0082/0083 must not be modified, and this
column genuinely wasn't part of the chunk-1 brief — 0082's `customer_name` /
`customer_phone` cover checkout identity, but chunk 3 additionally accepts an
OPTIONAL email so a LATER wave (the customer status-page link email,
explicitly out of scope for chunk 3 — no SENDING happens here) has somewhere
to read it from instead of the checkout silently discarding what the
customer typed. Reported loudly per the chunk-3 instructions.

Nullable, no backfill: no existing order has a captured email today.

Revision ID: 0084_orders_customer_email
Revises:     0083_locations_phone
Create Date: 2026-09-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0084_orders_customer_email"
down_revision = "0083_locations_phone"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_email TEXT"))
    logger.info("0084: orders.customer_email added")


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE orders DROP COLUMN IF EXISTS customer_email"))
