"""Migrate billing_config from restaurants to organizations.

WHY:
  Wave 2 app code refactor centralizes Org-level settings on `organizations`.
  `billing_config` currently lives only on `restaurants`. To let app code
  query billing_config from organizations (and survive the forthcoming
  0037 rename), we mirror the column to organizations now and backfill.

  This is additive: `restaurants.billing_config` stays populated for now.
  After 0037 applies, `restaurants` becomes a VIEW that exposes
  `organizations.billing_config`, so downstream reads from `restaurants`
  still work.

Revision ID: 0037c_billing_conf
Revises: 0037b_recovery_cons
Create Date: 2026-04-18
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0037c_billing_conf"
down_revision = "0037b_recovery_cons"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    logger.info("0037c: adding billing_config to organizations")
    op.execute(sa.text(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS billing_config JSONB"
    ))

    # Backfill only if restaurants.billing_config exists in this deployment.
    result = op.get_bind().execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'restaurants'
          AND column_name = 'billing_config'
    """)).scalar()
    if result:
        logger.info("0037c: backfilling organizations.billing_config from restaurants")
        op.execute(sa.text("""
            UPDATE organizations o
            SET billing_config = r.billing_config
            FROM restaurants r
            WHERE o.id = r.id
              AND o.billing_config IS NULL
              AND r.billing_config IS NOT NULL
        """))
    else:
        logger.info("0037c: restaurants.billing_config does not exist — skipping backfill")


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS billing_config"
    ))
