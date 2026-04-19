"""Drop legacy compat artifacts: restaurants_deprecated, mapping table, is_primary, parent_restaurant_id

Wave-2 final cleanup. After Olas 1 + 2a (commits ad524bd + 1390d92)
refactored every SQL site away from the deprecated symbols, drop them
from the schema for good. Burn the compat layer.

Drops:
  - TABLE restaurants_deprecated CASCADE
        Legacy data dump from the Wave-2 transition. Not queried by any
        post-Wave-2 code.
  - TABLE _migration_restaurant_to_location CASCADE
        Mapping table used during 0035/0036 backfill. The lookup that
        depended on it was retired in commit f2465bc (auth.py + deps.py).
        Confirmed unused via repo grep.
  - INDEX ux_locations_org_primary (partial unique index)
        Enforced "one primary per org" — no longer meaningful.
  - COLUMN locations.is_primary
        Vestigial 0034 backfill flag. Every location is a peer post-Wave-2.
  - VIEW restaurants and recreate WITHOUT parent_restaurant_id
        The CASE WHEN l.is_primary THEN NULL ELSE l.org_id END computed
        column is no longer expressible (is_primary is gone) and was
        legacy emulation anyway. Dict accessors `restaurant.get(
        "parent_restaurant_id")` will return None — this gracefully
        falls into the "is matriz" branch in callers, which is the
        canonical Wave-2 model (every location is effectively a peer).

CALLER IMPACT (verified pre-migration):
  - All SQL filters / orders / writes on these symbols were removed in
    commits ad524bd (parent_restaurant_id) and 1390d92 (is_primary).
  - All SELECT projections of `is_primary` from app/repositories/
    restaurant_repo.py were stripped immediately before this migration.
  - Forward-guard tests (tests/test_no_parent_restaurant_id_sql.py and
    tests/test_no_is_primary_sql.py) prevent reintroduction.

NON-REVERSIBLE: restaurants_deprecated and _migration_restaurant_to_location
are data dumps; their contents are not preserved. is_primary requires
backfill from prior knowledge. This migration is forward-only — the
downgrade raises NotImplementedError to prevent accidental data loss.

Revision ID: 0038_drop_legacy_compat
Revises: 0037_drop_legacy_rls
Create Date: 2026-04-19
"""
import logging

import sqlalchemy as sa
from alembic import op


revision = "0038_drop_legacy_compat"
down_revision = "0037_drop_legacy_rls"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    # 1. Drop the legacy data-dump table.
    logger.info("0038: dropping restaurants_deprecated table (legacy)")
    op.execute(sa.text("DROP TABLE IF EXISTS restaurants_deprecated CASCADE"))

    # 2. Drop the migration mapping table (no callers since commit f2465bc).
    logger.info("0038: dropping _migration_restaurant_to_location table (mapping retired)")
    op.execute(sa.text("DROP TABLE IF EXISTS _migration_restaurant_to_location CASCADE"))

    # 3. Drop the restaurants VIEW first (depends on is_primary).
    logger.info("0038: dropping restaurants VIEW (will recreate without parent_restaurant_id)")
    op.execute(sa.text("DROP VIEW IF EXISTS restaurants"))

    # 4. Drop the partial unique index on (org_id) WHERE is_primary
    #    if it exists. Name varies between 0034 and 0035 — drop both.
    logger.info("0038: dropping is_primary partial unique indexes")
    op.execute(sa.text("DROP INDEX IF EXISTS ux_locations_org_primary"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_locations_org_primary"))

    # 5. Drop the is_primary column from locations.
    logger.info("0038: dropping locations.is_primary column")
    op.execute(sa.text("ALTER TABLE locations DROP COLUMN IF EXISTS is_primary"))

    # 6. Recreate the restaurants VIEW without parent_restaurant_id and
    #    without any reference to is_primary. Rest of the shape preserved
    #    for backward compat with code that SELECTs from the VIEW.
    logger.info("0038: recreating restaurants VIEW without legacy columns")
    op.execute(sa.text("""
        CREATE VIEW restaurants AS
        SELECT
            l.id,
            COALESCE(o.name, l.name)                              AS name,
            l.name                                                AS location_name,
            COALESCE(l.whatsapp_number, o.whatsapp_number)        AS whatsapp_number,
            l.address,
            l.latitude,
            l.longitude,
            o.menu,
            o.features,
            COALESCE(l.wa_phone_id,     o.wa_phone_id)            AS wa_phone_id,
            COALESCE(l.wa_access_token, o.wa_access_token)        AS wa_access_token,
            o.slug,
            o.billing_config,
            o.subscription_status,
            o.subscription_plan,
            l.created_at,
            l.updated_at
        FROM locations l
        JOIN organizations o ON o.id = l.org_id
    """))

    logger.info("0038: complete — legacy compat artifacts dropped")


def downgrade() -> None:
    """Forward-only migration.

    The dropped artifacts (restaurants_deprecated table, mapping table,
    is_primary column with backfilled values) cannot be reconstructed
    without the original data. If you need to roll back, restore from a
    pre-0038 database backup instead of running this downgrade.
    """
    raise NotImplementedError(
        "0038 is forward-only. Restore from a pre-0038 backup to undo. "
        "The dropped artifacts contain no recoverable data in the "
        "current schema."
    )
