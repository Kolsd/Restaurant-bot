"""Restaurant slug column for SEO routes.

Adds:
  - restaurants.slug TEXT NOT NULL UNIQUE
  - Backfill from name with duplicate resolution via rn suffix
  - Fallback to 'r-{id}' for NULL/empty names

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-14
"""

from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS slug TEXT;

        -- Backfill unique slugs from name, resolving duplicates with numeric suffix
        WITH numbered AS (
            SELECT
                id,
                lower(regexp_replace(coalesce(name, ''), '[^a-zA-Z0-9]+', '-', 'g')) AS base_slug,
                row_number() OVER (
                    PARTITION BY lower(regexp_replace(coalesce(name, ''), '[^a-zA-Z0-9]+', '-', 'g'))
                    ORDER BY id
                ) AS rn
            FROM restaurants
            WHERE slug IS NULL
        )
        UPDATE restaurants r
        SET slug = CASE
                      WHEN n.rn = 1 THEN trim(both '-' from n.base_slug)
                      ELSE trim(both '-' from n.base_slug) || '-' || n.rn::text
                   END
        FROM numbered n
        WHERE r.id = n.id;

        -- Restaurants with empty/null name get slug='r-{id}'
        UPDATE restaurants
        SET slug = 'r-' || id::text
        WHERE slug IS NULL OR slug = '' OR slug = '-';

        ALTER TABLE restaurants ALTER COLUMN slug SET NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_restaurants_slug ON restaurants(slug);
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ux_restaurants_slug;")
    op.execute("ALTER TABLE restaurants DROP COLUMN IF EXISTS slug;")
