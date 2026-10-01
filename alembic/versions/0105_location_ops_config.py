"""locations.ops_config: which operational screens a sede actually uses.

Every order showed on BOTH the kitchen and the bar screen: the bar split
(`features.bar_enabled` / `bar_categories`) existed in code but nothing in the
app could ever set it. Each sede is its own restaurant, so the answer lives on
the sede: does it have a bar (and which carta categories go there), does it
take delivery/pickup, does it have its own couriers, do its waiters use the
app. The owner answers once, the first time they open Operación
(app/services/ops_config.py).

'{}' = never configured: every screen shows, as before.

Revision ID: 0105_location_ops_config
Revises:     0104_diner_memory
Create Date: 2026-10-01
"""
from alembic import op

revision = "0105_location_ops_config"
down_revision = "0104_diner_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE locations ADD COLUMN IF NOT EXISTS ops_config JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE locations DROP COLUMN IF EXISTS ops_config")
