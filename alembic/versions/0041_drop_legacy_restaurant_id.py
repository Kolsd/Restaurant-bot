"""Drop locations.legacy_restaurant_id.

The column was added in 0034 as a migration helper (backfill mapping for
Wave-2 Org/Location restructuring).  All backfill code that consumed it
was removed in 0038.  No app code reads or writes this column post-0038
(verified: grep -rn legacy_restaurant_id app/ returns zero hits).

Upgrade: simply drops the column.
Downgrade: re-adds the column as NULL (historical mapping data is gone).

Revision ID: 0041_drop_legacy_rid
"""

import logging

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

# revision identifiers, used by Alembic.
revision = "0041_drop_legacy_rid"
down_revision = "0039_channel_attr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    logger.info("0041 upgrade: dropping locations.legacy_restaurant_id")
    op.execute(
        "ALTER TABLE locations DROP COLUMN IF EXISTS legacy_restaurant_id"
    )
    logger.info("0041 upgrade: done")


def downgrade() -> None:
    logger.info("0041 downgrade: re-adding locations.legacy_restaurant_id as NULL")
    op.execute(
        "ALTER TABLE locations ADD COLUMN IF NOT EXISTS legacy_restaurant_id INTEGER NULL"
    )
    logger.info("0041 downgrade: done (historical mapping data not restored)")
