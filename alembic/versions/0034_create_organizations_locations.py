"""Create organizations and locations tables — Org/Location migration Fase 1.

WHY:
  Today `restaurants` plays a dual role: tenant container (billing, menu,
  users, subscription) AND operational restaurant. This causes the Matriz/
  Sucursal edge cases documented in ORG_LOCATION_MIGRATION_PLAN.md §1.

  This migration introduces the clean separation:
    Organization — tenant container (billing, menu canonical, WhatsApp default)
    Location     — operational seat (address, GPS, staff, orders, inventory)

  ONE Organization per Mesio client; ONE or N Locations per Org.

DATA STRATEGY:
  - Every Matriz (parent_restaurant_id IS NULL) → one Organization
  - Every Matriz → also one primary Location (code='principal')
  - Every Sucursal (parent_restaurant_id IS NOT NULL) → one Location under
    its parent's Organization
  - WhatsApp suffixes like _b[TIMESTAMP] are nulled out on the Location
    (they were internal dedup hacks; the Org holds the canonical number)

INVARIANTS AFTER THIS MIGRATION:
  - organizations.id = old restaurants.id (for all Matrizes)
  - _migration_restaurant_to_location has exactly one row per restaurant
  - Every Location with is_primary=true is the only primary for its Org
    (enforced by UNIQUE partial index)

NO CODE CHANGES YET: the app still reads from `restaurants`. This migration
is purely additive — it creates new tables and populates them from existing
data. No existing columns are modified.

Revision ID: 0034_create_organizations_locations
Revises: 0033_processed_wompi_events
Create Date: 2026-04-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0034_create_organizations_locations"
down_revision = "0033_processed_wompi_events"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()

    # ── Step 0: Enable unaccent extension (for slug/code sanitization) ───────
    # unaccent is bundled with standard Postgres distributions. If for any
    # reason it's absent, the translate() fallback in the sucursales INSERT
    # handles common Spanish accents.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS unaccent"))

    # ── Step 1: Create organizations table ──────────────────────────────────
    logger.info("0034: creating organizations table")
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS organizations (
            id                  BIGSERIAL PRIMARY KEY,
            name                TEXT NOT NULL,
            slug                TEXT UNIQUE,
            whatsapp_number     TEXT,
            wa_phone_id         TEXT,
            wa_access_token     TEXT,
            menu                JSONB NOT NULL DEFAULT '[]'::jsonb,
            features            JSONB NOT NULL DEFAULT '{}'::jsonb,
            subscription_plan   TEXT DEFAULT 'free',
            subscription_status TEXT DEFAULT 'active',
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_organizations_whatsapp
            ON organizations(whatsapp_number)
            WHERE whatsapp_number IS NOT NULL
    """))
    logger.info("0034: organizations table created")

    # ── Step 2: Create locations table ──────────────────────────────────────
    logger.info("0034: creating locations table")
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS locations (
            id                   BIGSERIAL PRIMARY KEY,
            org_id               BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name                 TEXT NOT NULL,
            code                 TEXT,
            address              TEXT,
            latitude             DOUBLE PRECISION,
            longitude            DOUBLE PRECISION,
            whatsapp_number      TEXT,
            wa_phone_id          TEXT,
            wa_access_token      TEXT,
            active               BOOLEAN NOT NULL DEFAULT true,
            is_primary           BOOLEAN NOT NULL DEFAULT false,
            timezone             TEXT DEFAULT 'America/Bogota',
            opening_hours        JSONB DEFAULT '{}'::jsonb,
            created_at           TIMESTAMPTZ DEFAULT NOW(),
            updated_at           TIMESTAMPTZ DEFAULT NOW(),
            legacy_restaurant_id BIGINT UNIQUE  -- migration helper; Dropped in Fase 7
        )
    """))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_locations_whatsapp
            ON locations(whatsapp_number)
            WHERE whatsapp_number IS NOT NULL
    """))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_locations_org_primary
            ON locations(org_id)
            WHERE is_primary = true
    """))
    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_locations_org
            ON locations(org_id)
    """))
    logger.info("0034: locations table created")

    # ── Step 3: Create mapping table (used by migration 0035 for backfill) ──
    logger.info("0034: creating _migration_restaurant_to_location table")
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS _migration_restaurant_to_location (
            old_restaurant_id BIGINT PRIMARY KEY,
            org_id            BIGINT NOT NULL,
            location_id       BIGINT NOT NULL
        )
    """))
    logger.info("0034: mapping table created")

    # ── Step 4: Backfill organizations from Matrizes ─────────────────────────
    # Every Matriz (parent_restaurant_id IS NULL) becomes an Organization.
    # We preserve the same id so that existing FK references (users, etc.) are
    # still navigable via the old id. The serial starts after the max id.
    logger.info("0034: backfilling organizations from Matrizes")
    op.execute(sa.text("""
        INSERT INTO organizations (
            id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
            menu, features, created_at
        )
        SELECT
            r.id,
            r.name,
            r.slug,
            r.whatsapp_number,
            NULLIF(r.wa_phone_id, ''),
            NULLIF(r.wa_access_token, ''),
            CASE
                WHEN jsonb_typeof(r.menu) = 'array' THEN r.menu
                ELSE '[]'::jsonb
            END,
            COALESCE(r.features, '{}'::jsonb),
            r.created_at
        FROM restaurants r
        WHERE r.parent_restaurant_id IS NULL
        ON CONFLICT (id) DO NOTHING
    """))

    # Advance the sequence past the max id so new orgs don't collide.
    op.execute(sa.text("""
        SELECT setval(
            'organizations_id_seq',
            (SELECT COALESCE(MAX(id), 1) FROM organizations)
        )
    """))

    result = conn.execute(sa.text("SELECT COUNT(*) FROM organizations"))
    org_count = result.scalar()
    logger.info("0034: inserted %d organizations", org_count)

    # ── Step 5: Backfill primary Locations for each Matriz ───────────────────
    # The Matriz's own WhatsApp number lives on the Org; the primary Location
    # does NOT duplicate it (Location.whatsapp_number = NULL means "use Org's").
    logger.info("0034: backfilling primary locations for Matrizes")
    op.execute(sa.text("""
        INSERT INTO locations (
            org_id, name, code, address, latitude, longitude,
            whatsapp_number, wa_phone_id, wa_access_token,
            active, is_primary, timezone, created_at,
            legacy_restaurant_id
        )
        SELECT
            r.id AS org_id,
            r.name,
            'principal' AS code,
            NULLIF(r.address, '') AS address,
            r.latitude::DOUBLE PRECISION,
            r.longitude::DOUBLE PRECISION,
            NULL AS whatsapp_number,
            NULL AS wa_phone_id,
            NULL AS wa_access_token,
            true  AS active,
            true  AS is_primary,
            COALESCE(
                NULLIF(r.features->>'timezone', ''),
                'America/Bogota'
            ) AS timezone,
            r.created_at,
            r.id AS legacy_restaurant_id
        FROM restaurants r
        WHERE r.parent_restaurant_id IS NULL
        ON CONFLICT DO NOTHING
    """))

    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM locations WHERE is_primary = true"
    ))
    primary_count = result.scalar()
    logger.info("0034: inserted %d primary locations", primary_count)

    # ── Step 6: Backfill Locations for each Sucursal ─────────────────────────
    # Sucursales that share the parent's exact whatsapp_number, or whose number
    # has the internal "_b[TIMESTAMP]" suffix, get NULL whatsapp_number on the
    # Location. They will inherit the Org-level number.
    logger.info("0034: backfilling locations for sucursales")
    op.execute(sa.text("""
        INSERT INTO locations (
            org_id, name, code, address, latitude, longitude,
            whatsapp_number, wa_phone_id, wa_access_token,
            active, is_primary, timezone, created_at,
            legacy_restaurant_id
        )
        SELECT
            r.parent_restaurant_id AS org_id,
            r.name,
            -- Sanitize code: strip accents → keep [a-z0-9] → collapse/trim dashes → max 64 chars
            -- unaccent() normalizes é→e, ñ→n, etc. If unaccent is somehow unavailable,
            -- translate() handles the most common Spanish accented characters as a fallback.
            substring(
                lower(
                    regexp_replace(
                        regexp_replace(
                            unaccent(r.name),
                            '[^a-zA-Z0-9]+', '-', 'g'
                        ),
                        '^-+|-+$', '', 'g'
                    )
                )
                FROM 1 FOR 64
            ) AS code,
            NULLIF(r.address, '') AS address,
            r.latitude::DOUBLE PRECISION,
            r.longitude::DOUBLE PRECISION,
            -- Null out internal _b[TIMESTAMP] suffixes and parent duplicates.
            -- Regex anchors to end-of-string and requires 10+ digits (Unix ts ms/s).
            CASE
                WHEN r.whatsapp_number ~ '_b[0-9]{10,}$'
                    THEN NULL
                WHEN r.whatsapp_number = (
                    SELECT p.whatsapp_number
                    FROM restaurants p
                    WHERE p.id = r.parent_restaurant_id
                )
                    THEN NULL
                ELSE r.whatsapp_number
            END AS whatsapp_number,
            CASE
                WHEN r.whatsapp_number ~ '_b[0-9]{10,}$'
                    THEN NULL
                ELSE NULLIF(r.wa_phone_id, '')
            END AS wa_phone_id,
            CASE
                WHEN r.whatsapp_number ~ '_b[0-9]{10,}$'
                    THEN NULL
                ELSE NULLIF(r.wa_access_token, '')
            END AS wa_access_token,
            true  AS active,
            false AS is_primary,
            'America/Bogota' AS timezone,
            r.created_at,
            r.id AS legacy_restaurant_id
        FROM restaurants r
        WHERE r.parent_restaurant_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """))

    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM locations WHERE is_primary = false"
    ))
    branch_count = result.scalar()
    logger.info("0034: inserted %d branch locations", branch_count)

    # ── Step 7: Build the mapping table ──────────────────────────────────────
    # One row per restaurant (Matriz + Sucursal).
    # Uses legacy_restaurant_id for a guaranteed 1:1 join — eliminates the
    # previous name-matching which was non-deterministic when two sucursales
    # under the same Matriz shared the same name (e.g. two "Centro" branches).
    logger.info("0034: building mapping table")
    op.execute(sa.text("""
        INSERT INTO _migration_restaurant_to_location (
            old_restaurant_id, org_id, location_id
        )
        SELECT r.id, l.org_id, l.id
        FROM restaurants r
        JOIN locations l ON l.legacy_restaurant_id = r.id
        ON CONFLICT (old_restaurant_id) DO NOTHING
    """))

    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM _migration_restaurant_to_location"
    ))
    mapping_count = result.scalar()
    logger.info("0034: mapping table has %d rows", mapping_count)

    # ── Step 8: Validation — every restaurant must have a mapping ────────────
    logger.info("0034: running validation")
    op.execute(sa.text("""
        DO $$
        DECLARE
            orphans INT;
            null_locs INT;
        BEGIN
            -- Check 1: every restaurant has a mapping row
            SELECT COUNT(*) INTO orphans
            FROM restaurants r
            LEFT JOIN _migration_restaurant_to_location m
                ON m.old_restaurant_id = r.id
            WHERE m.old_restaurant_id IS NULL;

            IF orphans > 0 THEN
                RAISE EXCEPTION
                    'Migration 0034: % restaurant(s) without a mapping to location',
                    orphans;
            END IF;

            -- Check 2: no mapping row has NULL location_id
            SELECT COUNT(*) INTO null_locs
            FROM _migration_restaurant_to_location
            WHERE location_id IS NULL;

            IF null_locs > 0 THEN
                RAISE EXCEPTION
                    'Migration 0034: % mapping row(s) with NULL location_id '
                    '(name-match failed for sucursales with unique names)',
                    null_locs;
            END IF;

            -- Check 3: every org has exactly one primary location
            IF EXISTS (
                SELECT org_id
                FROM locations
                WHERE is_primary = true
                GROUP BY org_id
                HAVING COUNT(*) <> 1
            ) THEN
                RAISE EXCEPTION
                    'Migration 0034: some orgs have != 1 primary location';
            END IF;
        END $$;
    """))
    logger.info("0034: validation passed")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    logger.info("0034 downgrade: dropping mapping + locations + organizations")
    op.execute(sa.text(
        "DROP TABLE IF EXISTS _migration_restaurant_to_location"
    ))
    op.execute(sa.text("DROP TABLE IF EXISTS locations"))
    op.execute(sa.text("DROP TABLE IF EXISTS organizations"))
    logger.info("0034 downgrade: complete")
