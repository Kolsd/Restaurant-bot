"""Menu events table for catalog v2 heatmap analytics.

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-14
"""
from alembic import op

revision      = "0024"
down_revision = "0023"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS menu_events (
            id            BIGSERIAL PRIMARY KEY,
            restaurant_id INT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            dish_name     TEXT NOT NULL,
            event_type    TEXT NOT NULL CHECK (event_type IN ('view','modal_open','add_to_cart','ordered')),
            phone         TEXT NULL,
            bot_number    TEXT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS ix_menu_events_restaurant_created
            ON menu_events(restaurant_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS ix_menu_events_dish
            ON menu_events(restaurant_id, dish_name, event_type);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS menu_events CASCADE;")
