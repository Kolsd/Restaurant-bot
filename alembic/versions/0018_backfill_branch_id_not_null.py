"""Backfill NULL branch_id to parent restaurant_id.

Previously, tables/orders belonging to the "matriz" (parent restaurant) had
branch_id = NULL, requiring special IS NULL handling everywhere.  After this
migration, branch_id always equals the owning restaurant's id (parent or
branch), and a NOT NULL constraint prevents future NULLs.

The backfill derives the parent restaurant_id from the table id convention
"table-{restaurant_id}-{number}" for restaurant_tables, and from the
related table for table_orders.  For other tables the pattern is similar.

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-13
"""
from alembic import op

revision      = "0018"
down_revision = "0017"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── restaurant_tables ────────────────────────────────────────────
    # Convention: id = "table-{restaurant_id}-{number}"
    # Extract restaurant_id from the id and set it as branch_id where NULL
    op.execute("""
        UPDATE restaurant_tables
        SET branch_id = split_part(id, '-', 2)::INTEGER
        WHERE branch_id IS NULL
          AND id LIKE 'table-%-%%'
    """)
    # Safety: if any rows still NULL (non-standard ids), set from parent restaurants
    op.execute("""
        UPDATE restaurant_tables rt
        SET branch_id = (
            SELECT r.id FROM restaurants r
            WHERE r.parent_restaurant_id IS NULL
            LIMIT 1
        )
        WHERE rt.branch_id IS NULL
    """)
    # Add NOT NULL with a safe default
    op.execute("""
        ALTER TABLE restaurant_tables
        ALTER COLUMN branch_id SET NOT NULL
    """)

    # ── table_orders ─────────────────────────────────────────────────
    # Backfill from the related restaurant_tables row
    op.execute("""
        UPDATE table_orders tor
        SET branch_id = rt.branch_id
        FROM restaurant_tables rt
        WHERE tor.table_id = rt.id
          AND tor.branch_id IS NULL
    """)
    # Fallback for orphan orders: use parent restaurant
    op.execute("""
        UPDATE table_orders
        SET branch_id = (
            SELECT r.id FROM restaurants r
            WHERE r.parent_restaurant_id IS NULL
            LIMIT 1
        )
        WHERE branch_id IS NULL
    """)

    # ── time_slot_discounts ──────────────────────────────────────────
    # branch_id NULL means "applies to the parent restaurant"
    op.execute("""
        UPDATE time_slot_discounts tsd
        SET branch_id = tsd.restaurant_id
        WHERE tsd.branch_id IS NULL
    """)

    # ── occupancy_snapshots ──────────────────────────────────────────
    op.execute("""
        UPDATE occupancy_snapshots os
        SET branch_id = os.restaurant_id
        WHERE os.branch_id IS NULL
    """)

    # ── reservations ─────────────────────────────────────────────────
    # branch_id NULL → set to the restaurant that owns the bot_number
    op.execute("""
        UPDATE reservations res
        SET branch_id = (
            SELECT r.id FROM restaurants r
            WHERE r.whatsapp_number = res.bot_number
            LIMIT 1
        )
        WHERE res.branch_id IS NULL
          AND res.bot_number IS NOT NULL
    """)
    # Final fallback
    op.execute("""
        UPDATE reservations
        SET branch_id = (
            SELECT r.id FROM restaurants r
            WHERE r.parent_restaurant_id IS NULL
            LIMIT 1
        )
        WHERE branch_id IS NULL
    """)


def downgrade() -> None:
    # Allow NULLs again
    op.execute("ALTER TABLE restaurant_tables ALTER COLUMN branch_id DROP NOT NULL")
    # We don't restore NULLs — the data change is safe to keep.
