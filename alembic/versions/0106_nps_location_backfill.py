"""nps_responses.location_id for answers given at a table.

A dine-in NPS answer was saved with branch_id (the table's sede) but
location_id left NULL, so every per-sede NPS (Sucursales, the branches
comparison) came out empty for table service. The writers now store the
sede; this fills the rows already saved. Post-0057 branch_id IS the
location id; only ids that really are a sede of the same org are copied.

Revision ID: 0106_nps_location_backfill
Revises:     0105_location_ops_config
Create Date: 2026-10-01
"""
from alembic import op

revision = "0106_nps_location_backfill"
down_revision = "0105_location_ops_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE nps_responses n
           SET location_id = n.branch_id
          FROM locations l
         WHERE n.location_id IS NULL
           AND n.branch_id IS NOT NULL
           AND l.id = n.branch_id
           AND l.org_id = n.org_id
        """
    )


def downgrade() -> None:
    # Data-only: there is no way to tell backfilled rows from rows saved
    # correctly afterwards, and both are right. Nothing to undo.
    pass
