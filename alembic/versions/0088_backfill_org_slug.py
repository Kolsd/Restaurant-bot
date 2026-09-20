"""Backfill organizations.slug — a NULL slug has no public ordering link.

`/pedir/{slug}` (the whole web delivery/pickup channel) resolves the org via
delivery_repo.db_get_org_by_slug, and the sede config screen builds its
"public_link" from the same column. Orgs created after migration 0034 were
inserted with slug = NULL — CRM conversion never passed one — so every
customer onboarded since then has an unreachable ordering link.

db_create_organization now derives a slug when the caller omits one; this
migration does the same for the rows that already exist. Slugs are built
from the name and de-duplicated with a numeric suffix, matching
restaurant_repo._slugify / _unique_org_slug. Orgs whose name slugifies to
nothing fall back to `org-<id>`, which is always unique.

Revision ID: 0088_backfill_org_slug
Revises:     0087_orders_nps_answered_at
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0088_backfill_org_slug"
down_revision = "0087_orders_nps_answered_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # regexp_replace mirrors _slugify: non-alphanumeric runs -> '-', trimmed.
    # row_number() over the same base gives deterministic '-2', '-3' suffixes
    # for names that collide; the WHERE keeps already-slugged orgs untouched.
    op.execute(sa.text("""
        WITH candidates AS (
            SELECT
                id,
                COALESCE(
                    NULLIF(
                        trim(BOTH '-' FROM regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g')),
                        ''
                    ),
                    'org-' || id::text
                ) AS base
            FROM organizations
            WHERE slug IS NULL OR trim(slug) = ''
        ),
        numbered AS (
            SELECT
                id,
                left(base, 60) AS base,
                row_number() OVER (PARTITION BY left(base, 60) ORDER BY id) AS rn
            FROM candidates
        )
        UPDATE organizations o
           SET slug = CASE WHEN n.rn = 1 THEN n.base ELSE n.base || '-' || n.rn::text END
          FROM numbered n
         WHERE o.id = n.id
           AND NOT EXISTS (
                 SELECT 1 FROM organizations x
                  WHERE x.slug = CASE WHEN n.rn = 1 THEN n.base ELSE n.base || '-' || n.rn::text END
               )
    """))

    # Anything still NULL collided with a pre-existing slug — fall back to the
    # id, which cannot collide with a name-derived slug that starts with a letter
    # only by coincidence, and is unique by construction.
    op.execute(sa.text("""
        UPDATE organizations
           SET slug = 'org-' || id::text
         WHERE (slug IS NULL OR trim(slug) = '')
           AND NOT EXISTS (
                 SELECT 1 FROM organizations x WHERE x.slug = 'org-' || organizations.id::text
               )
    """))


def downgrade() -> None:
    # Slugs are public URLs once handed out; clearing them would break links
    # that customers already have. Intentionally a no-op.
    pass
