"""Add org_id and location_id columns to all tenant-scoped tables — Fase 2.

WHY:
  Migration 0034 created organizations and locations tables and populated them
  from existing restaurants data. This migration propagates the new foreign
  keys (org_id, location_id) to every tenant-scoped table so that application
  code can start referencing the new model.

STRATEGY:
  1. ADD COLUMN IF NOT EXISTS (nullable — instant, no table rewrite)
  2. Batched UPDATE via _migration_restaurant_to_location (10k rows at a time)
  3. Special COALESCE logic for tables that already have branch_id: prefer
     mapping from branch_id over restaurant_id to pick the correct Location
  4. SET NOT NULL on org_id for ALL tables (every row must have an Org)
  5. SET NOT NULL on location_id only for Location-level tables that are
     NOT designated nullable in the plan (carts, conversations, nps_*, etc.)
  6. ADD FK REFERENCES + CREATE INDEX
  7. Validation DO $$ RAISE EXCEPTION block

TABLES AFFECTED (33 RLS tables + 3 extras):
  All 33 tables from migration 0029 (_RLS_TABLES), plus:
  - restaurant_tables   (has branch_id, not restaurant_id directly)
  - reservations        (has branch_id + nullable restaurant_id)
  - reservation_deposits (FK→reservations, no direct restaurant_id — skip;
                          resolved via reservations.restaurant_id join)

  NOTE: webauthn_credentials is skipped — it has no restaurant_id column,
  scoped exclusively through staff_id FK → staff.restaurant_id.

LOCATION_ID NULLABILITY:
  The following tables have NULLABLE location_id by design (rows start
  without a resolved Location and get one lazily):
    carts, conversations, loyalty_ledger, menu_events, nps_responses,
    nps_waiting, payroll_runs, weekly_reports

  All other Location-level tables have NOT NULL location_id.

NO RLS POLICY CHANGES: that is migration 0036 (Bloque S2).
NO restaurant_id DROPS: those happen in migration 0037 (Bloque S6).

Revision ID: 0035_add_org_location_columns
Revises: 0034_org_locations
Create Date: 2026-04-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0035_add_org_location_columns"
down_revision = "0034_org_locations"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_BATCH = 10_000

# ---------------------------------------------------------------------------
# Table classification
# ---------------------------------------------------------------------------
# (table_name, needs_location_id, location_id_nullable, has_branch_id)
#
# has_branch_id = True means the table has an existing branch_id column that
# should be preferred when resolving location_id (COALESCE pattern from plan).

_ORG_ONLY_TABLES = [
    # (table_name)
    "billing_log",
    "contract_templates",
    "customer_profiles",
    "dish_recipes",
    "loyalty_customers",
    "marketing_messages_log",
    "subscription_usage",
]

# Tables that get BOTH org_id and location_id.
# Schema: (table_name, location_id_nullable, has_branch_id)
_ORG_AND_LOCATION_TABLES = [
    # Location-level, NOT NULL location_id, no branch_id
    ("attendance_deductions",   False, False),
    ("fiscal_invoices",         False, False),
    ("fiscal_resolution",       False, False),
    ("inventory",               False, False),
    ("menu_availability",       False, False),
    ("occupancy_snapshots",     False, True),   # has branch_id per 0017/0018
    ("overtime_requests",       False, False),
    ("staff",                   False, False),
    ("staff_deduction_items",   False, False),
    ("staff_schedules",         False, False),
    ("staff_shifts",            False, False),
    ("table_sessions",          False, False),  # no branch_id column in schema
    ("time_slot_discounts",     False, True),   # has branch_id (per 0015/0018)
    ("tip_distributions",       False, False),
    ("waiter_alerts",           False, False),  # no branch_id column in schema
    ("webauthn_challenges",     False, False),
    # Location-level, NOT NULL, branch_id
    ("orders",                  False, False),  # no branch_id column in schema
    ("table_orders",            False, True),   # branch_id per 0013/0018
    # Org-level but with nullable location_id
    ("carts",                   True,  False),  # no branch_id column; location nullable
    ("conversations",           True,  True),   # branch_id via 0026/0027
    ("loyalty_ledger",          True,  False),
    ("menu_events",             True,  False),
    ("nps_responses",           True,  True),   # branch_id via 0026/0027
    ("nps_waiting",             True,  False),  # no branch_id column in schema
    ("payroll_runs",            True,  False),
    ("weekly_reports",          True,  False),
    # Extra tables outside RLS set (plan §2.3)
    ("restaurant_tables",       False, True),   # branch_id is the tenant key; no restaurant_id
    ("reservations",            True,  True),   # branch_id per 0014/0018; restaurant_id nullable
]

# Tables where location_id is NOT NULL (derived from above)
_NOT_NULL_LOCATION_TABLES = {
    t for t, nullable, _ in _ORG_AND_LOCATION_TABLES
    if not nullable
}

# Tables with branch_id (derived from above)
_BRANCH_ID_TABLES = {
    t for t, _, has_branch in _ORG_AND_LOCATION_TABLES
    if has_branch
}

# restaurant_tables uses branch_id as its primary tenant key (no restaurant_id column).
# We backfill org_id and location_id via the branch_id → mapping join.
_BRANCH_ID_ONLY_TABLES = {"restaurant_tables"}

# Tables with nullable location_id (derived from above)
_NULLABLE_LOCATION_TABLES = {
    t for t, nullable, _ in _ORG_AND_LOCATION_TABLES
    if nullable
}

# ---------------------------------------------------------------------------
# Helper: batched backfill
# ---------------------------------------------------------------------------


def _batched_backfill_org_only(conn, table: str) -> int:
    """Backfill org_id for Org-level tables (restaurant_id → org_id via mapping)."""
    total = 0
    iteration = 0
    while True:
        iteration += 1
        result = conn.execute(sa.text(
            f"""
            WITH batch AS (
                SELECT ctid
                FROM {table}
                WHERE org_id IS NULL
                LIMIT {_BATCH}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {table} t
            SET org_id = m.org_id
            FROM _migration_restaurant_to_location m, batch
            WHERE t.ctid = batch.ctid
              AND t.restaurant_id = m.old_restaurant_id
            RETURNING 1
            """
        ))
        rows = result.rowcount
        total += rows
        if rows > 0:
            logger.info(
                "0035 backfill %s org_id: iteration %d updated %d rows (total: %d)",
                table, iteration, rows, total,
            )
        if rows == 0:
            break
    logger.info("0035 backfill %s org_id: DONE — %d rows", table, total)
    return total


def _batched_backfill_org_and_location(
    conn, table: str, has_branch_id: bool
) -> int:
    """Backfill org_id + location_id for Location-level tables.

    For tables with branch_id, we use COALESCE to prefer the branch mapping:
      - If branch_id is set → use mapping for branch_id (gives correct Location)
      - Fallback → use mapping for restaurant_id

    For restaurant_tables there is no restaurant_id column; it uses branch_id only.
    """
    total = 0
    iteration = 0
    is_branch_only = table in _BRANCH_ID_ONLY_TABLES
    while True:
        iteration += 1
        if is_branch_only:
            # restaurant_tables: no restaurant_id column; join via branch_id only
            query = sa.text(
                f"""
                WITH batch AS (
                    SELECT ctid
                    FROM {table}
                    WHERE org_id IS NULL
                    LIMIT {_BATCH}
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE {table} t
                SET org_id      = m.org_id,
                    location_id = m.location_id
                FROM _migration_restaurant_to_location m, batch
                WHERE t.ctid       = batch.ctid
                  AND t.branch_id  = m.old_restaurant_id
                RETURNING 1
                """
            )
        elif has_branch_id:
            # COALESCE pattern: prefer branch mapping, fall back to restaurant_id mapping.
            # Resolution is done in a CTE because Postgres does not allow the UPDATE
            # target alias `t` to be referenced from inside a FROM-clause JOIN's ON.
            query = sa.text(
                f"""
                WITH batch AS (
                    SELECT ctid, restaurant_id, branch_id
                    FROM {table}
                    WHERE org_id IS NULL
                    LIMIT {_BATCH}
                    FOR UPDATE SKIP LOCKED
                ),
                resolved AS (
                    SELECT b.ctid,
                           COALESCE(mb.org_id,      mr.org_id)      AS org_id,
                           COALESCE(mb.location_id, mr.location_id) AS location_id
                    FROM batch b
                    JOIN _migration_restaurant_to_location mr
                      ON mr.old_restaurant_id = b.restaurant_id
                    LEFT JOIN _migration_restaurant_to_location mb
                      ON mb.old_restaurant_id = b.branch_id
                )
                UPDATE {table} t
                SET org_id      = r.org_id,
                    location_id = r.location_id
                FROM resolved r
                WHERE t.ctid = r.ctid
                RETURNING 1
                """
            )
        else:
            query = sa.text(
                f"""
                WITH batch AS (
                    SELECT ctid
                    FROM {table}
                    WHERE org_id IS NULL
                    LIMIT {_BATCH}
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE {table} t
                SET org_id      = m.org_id,
                    location_id = m.location_id
                FROM _migration_restaurant_to_location m, batch
                WHERE t.ctid          = batch.ctid
                  AND t.restaurant_id = m.old_restaurant_id
                RETURNING 1
                """
            )
        result = conn.execute(query)
        rows = result.rowcount
        total += rows
        if rows > 0:
            logger.info(
                "0035 backfill %s org+loc: iteration %d updated %d rows (total: %d)",
                table, iteration, rows, total,
            )
        if rows == 0:
            break
    logger.info("0035 backfill %s org+loc: DONE — %d rows", table, total)
    return total


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()

    # ── Phase 1: ADD COLUMNS (nullable — instant) ────────────────────────────
    logger.info("0035: adding org_id columns to org-only tables")
    for table in _ORG_ONLY_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS org_id BIGINT"
        ))
        logger.info("0035: ADD COLUMN org_id on %s", table)

    logger.info("0035: adding org_id + location_id to org+location tables")
    for table, nullable, _ in _ORG_AND_LOCATION_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS org_id BIGINT"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS location_id BIGINT"
        ))
        logger.info("0035: ADD COLUMN org_id + location_id on %s", table)

    # ── Phase 2: Backfill ────────────────────────────────────────────────────
    logger.info("0035: backfilling org-only tables")
    for table in _ORG_ONLY_TABLES:
        try:
            _batched_backfill_org_only(conn, table)
        except Exception as exc:
            raise RuntimeError(
                f"0035 upgrade: backfill failed for org-only table '{table}': {exc}"
            ) from exc

    logger.info("0035: backfilling org+location tables")
    for table, _nullable, has_branch in _ORG_AND_LOCATION_TABLES:
        try:
            _batched_backfill_org_and_location(conn, table, has_branch)
        except Exception as exc:
            raise RuntimeError(
                f"0035 upgrade: backfill failed for org+location table '{table}': {exc}"
            ) from exc

    # ── Mop-up: reservations rows where restaurant_id IS NULL ────────────────
    # reservations.restaurant_id is nullable (added in 0014 with DEFAULT NULL).
    # The main backfill pass misses these; resolve purely via branch_id fallback.
    logger.info("0035: mop-up pass for reservations with NULL restaurant_id")
    total_mop = 0
    mop_iter = 0
    while True:
        mop_iter += 1
        mop_result = conn.execute(sa.text(
            f"""
            WITH batch AS (
                SELECT ctid
                FROM reservations
                WHERE org_id IS NULL
                LIMIT {_BATCH}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE reservations res
            SET org_id      = m.org_id,
                location_id = m.location_id
            FROM _migration_restaurant_to_location m, batch
            WHERE res.ctid    = batch.ctid
              AND res.branch_id = m.old_restaurant_id
            RETURNING 1
            """
        ))
        mop_rows = mop_result.rowcount
        total_mop += mop_rows
        if mop_rows > 0:
            logger.info(
                "0035 mop-up reservations: iteration %d updated %d rows",
                mop_iter, mop_rows,
            )
        if mop_rows == 0:
            break
    logger.info("0035 mop-up reservations: DONE — %d rows", total_mop)

    # ── Phase 3: Orphan cleanup ──────────────────────────────────────────────
    # Any row whose restaurant_id (or branch_id) does not map to any row in
    # _migration_restaurant_to_location references a deleted/non-existent
    # restaurant — dead data that cannot be reached by application code.
    # We DELETE these rows rather than block the migration. Safety ceiling:
    # if orphan count in a single table > _ORPHAN_MAX, we still raise.
    _ORPHAN_MAX = 100

    def _cleanup_orphans(tbl: str, column: str) -> None:
        result = conn.execute(sa.text(
            f"SELECT COUNT(*) FROM {tbl} WHERE {column} IS NULL"
        ))
        count = result.scalar() or 0
        if count == 0:
            logger.info("0035: %s — %s 0 orphans OK", tbl, column)
            return
        if count > _ORPHAN_MAX:
            raise RuntimeError(
                f"0035: table '{tbl}' has {count} rows with {column} IS NULL "
                f"after backfill (> {_ORPHAN_MAX}) — refusing to auto-delete. "
                f"Investigate: SELECT restaurant_id, COUNT(*) FROM {tbl} "
                f"WHERE {column} IS NULL GROUP BY 1 ORDER BY 2 DESC;"
            )
        # Log a sample of orphan restaurant_ids / branch_ids so they show up in
        # deploy logs. Pre-check column existence via information_schema to avoid
        # aborting the current transaction on a missing column.
        sample_col = None
        for candidate in ("restaurant_id", "branch_id"):
            has_col = conn.execute(sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "  AND table_name = :t AND column_name = :c"
            ), {"t": tbl, "c": candidate}).scalar()
            if has_col:
                sample_col = candidate
                break
        if sample_col:
            sample = conn.execute(sa.text(
                f"SELECT DISTINCT {sample_col} FROM {tbl} "
                f"WHERE {column} IS NULL LIMIT 10"
            )).fetchall()
            logger.warning(
                "0035 orphan cleanup: %s has %d orphan rows (sample %s: %s)",
                tbl, count, sample_col, [r[0] for r in sample],
            )
        else:
            logger.warning(
                "0035 orphan cleanup: %s has %d orphan rows (no restaurant_id/branch_id column)",
                tbl, count,
            )
        conn.execute(sa.text(f"DELETE FROM {tbl} WHERE {column} IS NULL"))
        logger.warning("0035 orphan cleanup: deleted %d rows from %s", count, tbl)

    logger.info("0035: cleaning up orphan org_id NULLs")
    for table in _ORG_ONLY_TABLES:
        _cleanup_orphans(table, "org_id")

    for table, _nullable, _ in _ORG_AND_LOCATION_TABLES:
        _cleanup_orphans(table, "org_id")
        # For NOT NULL location tables, also clean rows missing location_id
        if table not in _NULLABLE_LOCATION_TABLES:
            _cleanup_orphans(table, "location_id")

    # ── Phase 4: SET NOT NULL on org_id (all tables) ─────────────────────────
    logger.info("0035: enforcing NOT NULL on org_id")
    for table in _ORG_ONLY_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN org_id SET NOT NULL"
        ))
        logger.info("0035: org_id SET NOT NULL on %s", table)

    for table, _nullable, _ in _ORG_AND_LOCATION_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN org_id SET NOT NULL"
        ))
        logger.info("0035: org_id SET NOT NULL on %s", table)

    # ── Phase 5: SET NOT NULL on location_id (Location-level tables only) ────
    logger.info("0035: enforcing NOT NULL on location_id for required tables")
    for table in _NOT_NULL_LOCATION_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN location_id SET NOT NULL"
        ))
        logger.info("0035: location_id SET NOT NULL on %s", table)

    # ── Phase 6: Add FK constraints ──────────────────────────────────────────
    # PostgreSQL does not support ADD CONSTRAINT IF NOT EXISTS for FK constraints.
    # We use a DO block to check pg_constraint before adding, making it idempotent.
    logger.info("0035: adding FK constraints")
    for table in _ORG_ONLY_TABLES:
        op.execute(sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'fk_{table}_org'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table}
                    ADD CONSTRAINT fk_{table}_org
                    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        ))
        logger.info("0035: FK fk_%s_org added", table)

    for table, _nullable, _ in _ORG_AND_LOCATION_TABLES:
        op.execute(sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'fk_{table}_org'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table}
                    ADD CONSTRAINT fk_{table}_org
                    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        ))
        op.execute(sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'fk_{table}_location'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table}
                    ADD CONSTRAINT fk_{table}_location
                    FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        ))
        logger.info("0035: FKs fk_%s_org + fk_%s_location added", table, table)

    # ── Phase 7: Create indexes ───────────────────────────────────────────────
    logger.info("0035: creating indexes")
    for table in _ORG_ONLY_TABLES:
        op.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_org_id ON {table}(org_id)"
        ))
        logger.info("0035: index ix_%s_org_id created", table)

    for table, _nullable, _ in _ORG_AND_LOCATION_TABLES:
        op.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_org_id ON {table}(org_id)"
        ))
        op.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_location_id ON {table}(location_id)"
        ))
        logger.info("0035: indexes ix_%s_org_id + ix_%s_location_id created", table, table)

    # ── Phase 8: reservation_deposits — backfill via reservations join ────────
    # reservation_deposits has no restaurant_id column; it only has a FK to
    # reservations. We need to derive org_id + location_id from the parent.
    logger.info("0035: processing reservation_deposits via reservations join")
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits "
        "ADD COLUMN IF NOT EXISTS org_id BIGINT"
    ))
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits "
        "ADD COLUMN IF NOT EXISTS location_id BIGINT"
    ))

    # Backfill in batches from reservations → mapping.
    # reservations.restaurant_id is nullable (added in 0014 with DEFAULT NULL).
    # We use COALESCE(restaurant_id, branch_id) to resolve: if restaurant_id is NULL
    # we fall back to branch_id (which is always populated after 0018 backfill).
    total_rd = 0
    iteration = 0
    while True:
        iteration += 1
        result = conn.execute(sa.text(
            f"""
            WITH batch AS (
                SELECT rd.ctid
                FROM reservation_deposits rd
                WHERE rd.org_id IS NULL
                LIMIT {_BATCH}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE reservation_deposits rd
            SET org_id      = COALESCE(mb.org_id,      mr.org_id),
                location_id = COALESCE(mb.location_id, mr.location_id)
            FROM batch
            JOIN reservations r ON r.id = rd.reservation_id
            -- Primary join: resolve via restaurant_id (preferred)
            LEFT JOIN _migration_restaurant_to_location mr
                ON mr.old_restaurant_id = r.restaurant_id
            -- Secondary join: resolve via branch_id (fallback for NULL restaurant_id)
            LEFT JOIN _migration_restaurant_to_location mb
                ON mb.old_restaurant_id = r.branch_id
            WHERE rd.ctid = batch.ctid
              -- At least one mapping must resolve
              AND COALESCE(mb.org_id, mr.org_id) IS NOT NULL
            RETURNING 1
            """
        ))
        rows = result.rowcount
        total_rd += rows
        if rows > 0:
            logger.info(
                "0035 backfill reservation_deposits: iteration %d updated %d rows",
                iteration, rows,
            )
        if rows == 0:
            break
    logger.info("0035 backfill reservation_deposits: DONE — %d rows", total_rd)

    # Orphan check for reservation_deposits
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM reservation_deposits WHERE org_id IS NULL"
    ))
    count = result.scalar()
    if count > 0:
        raise RuntimeError(
            f"0035: reservation_deposits has {count} rows with org_id IS NULL "
            f"— likely reservations with NULL restaurant_id. Resolve and re-run."
        )

    op.execute(sa.text(
        "ALTER TABLE reservation_deposits ALTER COLUMN org_id SET NOT NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits ALTER COLUMN location_id SET NOT NULL"
    ))
    op.execute(sa.text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_reservation_deposits_org'
                  AND conrelid = 'reservation_deposits'::regclass
            ) THEN
                ALTER TABLE reservation_deposits
                ADD CONSTRAINT fk_reservation_deposits_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    ))
    op.execute(sa.text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_reservation_deposits_location'
                  AND conrelid = 'reservation_deposits'::regclass
            ) THEN
                ALTER TABLE reservation_deposits
                ADD CONSTRAINT fk_reservation_deposits_location
                FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_reservation_deposits_org_id "
        "ON reservation_deposits(org_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_reservation_deposits_location_id "
        "ON reservation_deposits(location_id)"
    ))
    logger.info("0035: reservation_deposits org_id + location_id complete")

    # ── Phase 9: Final validation block ──────────────────────────────────────
    logger.info("0035: running final validation")
    op.execute(sa.text("""
        DO $$
        DECLARE
            bad INT;
        BEGIN
            -- Location-level tables that must have NOT NULL location_id
            SELECT COUNT(*) INTO bad FROM orders           WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: orders has % rows with org_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM orders           WHERE location_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: orders has % rows with location_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM staff            WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: staff has % rows with org_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM staff            WHERE location_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: staff has % rows with location_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM inventory        WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: inventory has % rows with org_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM inventory        WHERE location_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: inventory has % rows with location_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM table_orders     WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: table_orders has % rows with org_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM table_orders     WHERE location_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: table_orders has % rows with location_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM restaurant_tables WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: restaurant_tables has % rows with org_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM restaurant_tables WHERE location_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: restaurant_tables has % rows with location_id NULL', bad; END IF;

            -- Org-only tables
            SELECT COUNT(*) INTO bad FROM conversations    WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: conversations has % rows with org_id NULL', bad; END IF;

            SELECT COUNT(*) INTO bad FROM billing_log      WHERE org_id IS NULL;
            IF bad > 0 THEN RAISE EXCEPTION '0035: billing_log has % rows with org_id NULL', bad; END IF;
        END $$;
    """))
    logger.info("0035: validation passed")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop constraints, indexes, columns from reservation_deposits first
    logger.info("0035 downgrade: cleaning reservation_deposits")
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_reservation_deposits_location_id"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_reservation_deposits_org_id"
    ))
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits "
        "DROP CONSTRAINT IF EXISTS fk_reservation_deposits_location"
    ))
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits "
        "DROP CONSTRAINT IF EXISTS fk_reservation_deposits_org"
    ))
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits DROP COLUMN IF EXISTS location_id"
    ))
    op.execute(sa.text(
        "ALTER TABLE reservation_deposits DROP COLUMN IF EXISTS org_id"
    ))

    # Drop from org+location tables (reverse order)
    for table, _nullable, _ in reversed(_ORG_AND_LOCATION_TABLES):
        op.execute(sa.text(
            f"DROP INDEX IF EXISTS ix_{table}_location_id"
        ))
        op.execute(sa.text(
            f"DROP INDEX IF EXISTS ix_{table}_org_id"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} "
            f"DROP CONSTRAINT IF EXISTS fk_{table}_location"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} "
            f"DROP CONSTRAINT IF EXISTS fk_{table}_org"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS location_id"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS org_id"
        ))
        logger.info("0035 downgrade: dropped org_id + location_id from %s", table)

    # Drop from org-only tables (reverse order)
    for table in reversed(_ORG_ONLY_TABLES):
        op.execute(sa.text(
            f"DROP INDEX IF EXISTS ix_{table}_org_id"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} "
            f"DROP CONSTRAINT IF EXISTS fk_{table}_org"
        ))
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS org_id"
        ))
        logger.info("0035 downgrade: dropped org_id from %s", table)

    logger.info("0035 downgrade: complete")
