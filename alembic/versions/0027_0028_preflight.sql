-- =============================================================================
-- Preflight script for migrations 0027 and 0028
-- =============================================================================
-- PURPOSE:
--   Run this manually in staging (or a production read replica) BEFORE applying
--   migrations 0027 and 0028. It is 100% read-only — no writes occur.
--
-- USAGE:
--   psql "$DATABASE_URL" -f alembic/versions/0027_0028_preflight.sql
--
-- OUTPUT:
--   For each affected table:
--     1. How many rows will be successfully backfilled (have a matching restaurant)
--     2. How many rows are orphans (no matching restaurant — MIGRATION WILL FAIL)
--     3. Up to 5 sample orphan bot_numbers for investigation
--
--   Also checks for duplicate whatsapp_number in restaurants (ambiguous JOIN).
--
-- DECISION GATE:
--   If any table shows orphan_count > 0, resolve those rows before running
--   the migration:
--     DELETE FROM <table> WHERE bot_number IN ('<orphan_1>', ...);
--   OR add the missing restaurant record.
--
--   If restaurants shows duplicate_count > 0, the JOIN in the migration will
--   assign an arbitrary restaurant_id. Deduplicate restaurants first.
-- =============================================================================

\pset format aligned
\pset linestyle unicode

-- =============================================================================
-- SECTION 0 — Duplicate whatsapp_number guard
-- =============================================================================
\echo ''
\echo '====================================================================='
\echo 'SECTION 0: Duplicate whatsapp_number in restaurants'
\echo '           (ambiguous JOIN — MUST be zero before running migrations)'
\echo '====================================================================='

SELECT
    whatsapp_number,
    COUNT(*) AS restaurant_count,
    array_agg(id ORDER BY id) AS restaurant_ids
FROM restaurants
WHERE whatsapp_number IS NOT NULL
GROUP BY whatsapp_number
HAVING COUNT(*) > 1
ORDER BY restaurant_count DESC, whatsapp_number;

-- If no rows: safe to proceed.

-- =============================================================================
-- SECTION 1 — Migration 0027 tables (restaurant_id column already exists,
--             currently nullable after 0026)
-- =============================================================================

\echo ''
\echo '====================================================================='
\echo 'SECTION 1: Migration 0027 — backfill counts (5 tables)'
\echo '====================================================================='

-- ── orders ──────────────────────────────────────────────────────────────────
\echo ''
\echo '--- orders ---'

SELECT
    'orders'                                            AS table_name,
    COUNT(*)                                            AS total_rows,
    COUNT(*) FILTER (WHERE o.restaurant_id IS NOT NULL) AS already_backfilled,
    COUNT(*) FILTER (
        WHERE o.restaurant_id IS NULL
          AND r.id IS NOT NULL
    )                                                   AS will_be_backfilled,
    COUNT(*) FILTER (
        WHERE o.restaurant_id IS NULL
          AND r.id IS NULL
    )                                                   AS orphan_count
FROM orders o
LEFT JOIN restaurants r ON r.whatsapp_number = o.bot_number;

\echo 'Sample orphan bot_numbers (orders):'
SELECT DISTINCT o.bot_number, COUNT(*) AS row_count
FROM orders o
LEFT JOIN restaurants r ON r.whatsapp_number = o.bot_number
WHERE o.restaurant_id IS NULL AND r.id IS NULL
GROUP BY o.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── table_orders ─────────────────────────────────────────────────────────────
\echo ''
\echo '--- table_orders ---'

SELECT
    'table_orders'                                       AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE t.restaurant_id IS NOT NULL)  AS already_backfilled,
    COUNT(*) FILTER (
        WHERE t.restaurant_id IS NULL
          AND r.id IS NOT NULL
    )                                                    AS will_be_backfilled,
    COUNT(*) FILTER (
        WHERE t.restaurant_id IS NULL
          AND r.id IS NULL
    )                                                    AS orphan_count
FROM table_orders t
LEFT JOIN restaurants r ON r.whatsapp_number = t.bot_number;

\echo 'Sample orphan bot_numbers (table_orders):'
SELECT DISTINCT t.bot_number, COUNT(*) AS row_count
FROM table_orders t
LEFT JOIN restaurants r ON r.whatsapp_number = t.bot_number
WHERE t.restaurant_id IS NULL AND r.id IS NULL
GROUP BY t.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── conversations ─────────────────────────────────────────────────────────────
\echo ''
\echo '--- conversations ---'

SELECT
    'conversations'                                      AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE c.restaurant_id IS NOT NULL)  AS already_backfilled,
    COUNT(*) FILTER (
        WHERE c.restaurant_id IS NULL
          AND r.id IS NOT NULL
    )                                                    AS will_be_backfilled,
    COUNT(*) FILTER (
        WHERE c.restaurant_id IS NULL
          AND r.id IS NULL
    )                                                    AS orphan_count
FROM conversations c
LEFT JOIN restaurants r ON r.whatsapp_number = c.bot_number;

\echo 'Sample orphan bot_numbers (conversations):'
SELECT DISTINCT c.bot_number, COUNT(*) AS row_count
FROM conversations c
LEFT JOIN restaurants r ON r.whatsapp_number = c.bot_number
WHERE c.restaurant_id IS NULL AND r.id IS NULL
GROUP BY c.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── nps_responses ─────────────────────────────────────────────────────────────
\echo ''
\echo '--- nps_responses ---'

SELECT
    'nps_responses'                                      AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE n.restaurant_id IS NOT NULL)  AS already_backfilled,
    COUNT(*) FILTER (
        WHERE n.restaurant_id IS NULL
          AND r.id IS NOT NULL
    )                                                    AS will_be_backfilled,
    COUNT(*) FILTER (
        WHERE n.restaurant_id IS NULL
          AND r.id IS NULL
    )                                                    AS orphan_count
FROM nps_responses n
LEFT JOIN restaurants r ON r.whatsapp_number = n.bot_number;

\echo 'Sample orphan bot_numbers (nps_responses):'
SELECT DISTINCT n.bot_number, COUNT(*) AS row_count
FROM nps_responses n
LEFT JOIN restaurants r ON r.whatsapp_number = n.bot_number
WHERE n.restaurant_id IS NULL AND r.id IS NULL
GROUP BY n.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── menu_availability ─────────────────────────────────────────────────────────
\echo ''
\echo '--- menu_availability ---'

SELECT
    'menu_availability'                                  AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE m.restaurant_id IS NOT NULL)  AS already_backfilled,
    COUNT(*) FILTER (
        WHERE m.restaurant_id IS NULL
          AND r.id IS NOT NULL
    )                                                    AS will_be_backfilled,
    COUNT(*) FILTER (
        WHERE m.restaurant_id IS NULL
          AND r.id IS NULL
    )                                                    AS orphan_count
FROM menu_availability m
LEFT JOIN restaurants r ON r.whatsapp_number = m.bot_number;

\echo 'Sample orphan bot_numbers (menu_availability):'
SELECT DISTINCT m.bot_number, COUNT(*) AS row_count
FROM menu_availability m
LEFT JOIN restaurants r ON r.whatsapp_number = m.bot_number
WHERE m.restaurant_id IS NULL AND r.id IS NULL
GROUP BY m.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- =============================================================================
-- SECTION 2 — Migration 0028 tables (restaurant_id does NOT exist yet)
-- =============================================================================

\echo ''
\echo '====================================================================='
\echo 'SECTION 2: Migration 0028 — backfill preview (4 tables)'
\echo '           (restaurant_id column does not yet exist in these tables)'
\echo '====================================================================='

-- ── carts ────────────────────────────────────────────────────────────────────
\echo ''
\echo '--- carts ---'

SELECT
    'carts'                                              AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE r.id IS NOT NULL)             AS will_be_backfilled,
    COUNT(*) FILTER (WHERE r.id IS NULL)                 AS orphan_count
FROM carts c
LEFT JOIN restaurants r ON r.whatsapp_number = c.bot_number;

\echo 'Sample orphan bot_numbers (carts):'
SELECT DISTINCT c.bot_number, COUNT(*) AS row_count
FROM carts c
LEFT JOIN restaurants r ON r.whatsapp_number = c.bot_number
WHERE r.id IS NULL
GROUP BY c.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── table_sessions ────────────────────────────────────────────────────────────
\echo ''
\echo '--- table_sessions ---'

SELECT
    'table_sessions'                                     AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE r.id IS NOT NULL)             AS will_be_backfilled,
    COUNT(*) FILTER (WHERE r.id IS NULL)                 AS orphan_count
FROM table_sessions ts
LEFT JOIN restaurants r ON r.whatsapp_number = ts.bot_number;

\echo 'Sample orphan bot_numbers (table_sessions):'
SELECT DISTINCT ts.bot_number, COUNT(*) AS row_count
FROM table_sessions ts
LEFT JOIN restaurants r ON r.whatsapp_number = ts.bot_number
WHERE r.id IS NULL
GROUP BY ts.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── waiter_alerts ─────────────────────────────────────────────────────────────
\echo ''
\echo '--- waiter_alerts ---'

SELECT
    'waiter_alerts'                                      AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE r.id IS NOT NULL)             AS will_be_backfilled,
    COUNT(*) FILTER (WHERE r.id IS NULL)                 AS orphan_count
FROM waiter_alerts wa
LEFT JOIN restaurants r ON r.whatsapp_number = wa.bot_number;

\echo 'Sample orphan bot_numbers (waiter_alerts):'
SELECT DISTINCT wa.bot_number, COUNT(*) AS row_count
FROM waiter_alerts wa
LEFT JOIN restaurants r ON r.whatsapp_number = wa.bot_number
WHERE r.id IS NULL
GROUP BY wa.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- ── nps_waiting ───────────────────────────────────────────────────────────────
\echo ''
\echo '--- nps_waiting ---'

SELECT
    'nps_waiting'                                        AS table_name,
    COUNT(*)                                             AS total_rows,
    COUNT(*) FILTER (WHERE r.id IS NOT NULL)             AS will_be_backfilled,
    COUNT(*) FILTER (WHERE r.id IS NULL)                 AS orphan_count
FROM nps_waiting nw
LEFT JOIN restaurants r ON r.whatsapp_number = nw.bot_number;

\echo 'Sample orphan bot_numbers (nps_waiting):'
SELECT DISTINCT nw.bot_number, COUNT(*) AS row_count
FROM nps_waiting nw
LEFT JOIN restaurants r ON r.whatsapp_number = nw.bot_number
WHERE r.id IS NULL
GROUP BY nw.bot_number
ORDER BY row_count DESC
LIMIT 5;

-- =============================================================================
-- SECTION 3 — Summary: go / no-go
-- =============================================================================

\echo ''
\echo '====================================================================='
\echo 'SECTION 3: Go/No-go summary'
\echo '           orphan_count must be 0 in ALL rows before running migration'
\echo '           duplicate_count must be 0 in SECTION 0 before running migration'
\echo '====================================================================='

-- Aggregated view across all 9 tables for a quick pass/fail read.
-- Note: for 0028 tables restaurant_id does not yet exist, so we compute
-- orphan status purely from the JOIN.

WITH checks AS (
    -- 0027 tables
    SELECT 'orders'           AS tbl, '0027' AS migration,
           COUNT(*) FILTER (WHERE r.id IS NULL AND o.restaurant_id IS NULL) AS orphans
    FROM orders o
    LEFT JOIN restaurants r ON r.whatsapp_number = o.bot_number
    UNION ALL
    SELECT 'table_orders', '0027',
           COUNT(*) FILTER (WHERE r.id IS NULL AND t.restaurant_id IS NULL)
    FROM table_orders t
    LEFT JOIN restaurants r ON r.whatsapp_number = t.bot_number
    UNION ALL
    SELECT 'conversations', '0027',
           COUNT(*) FILTER (WHERE r.id IS NULL AND c.restaurant_id IS NULL)
    FROM conversations c
    LEFT JOIN restaurants r ON r.whatsapp_number = c.bot_number
    UNION ALL
    SELECT 'nps_responses', '0027',
           COUNT(*) FILTER (WHERE r.id IS NULL AND n.restaurant_id IS NULL)
    FROM nps_responses n
    LEFT JOIN restaurants r ON r.whatsapp_number = n.bot_number
    UNION ALL
    SELECT 'menu_availability', '0027',
           COUNT(*) FILTER (WHERE r.id IS NULL AND m.restaurant_id IS NULL)
    FROM menu_availability m
    LEFT JOIN restaurants r ON r.whatsapp_number = m.bot_number
    -- 0028 tables
    UNION ALL
    SELECT 'carts', '0028',
           COUNT(*) FILTER (WHERE r.id IS NULL)
    FROM carts c2
    LEFT JOIN restaurants r ON r.whatsapp_number = c2.bot_number
    UNION ALL
    SELECT 'table_sessions', '0028',
           COUNT(*) FILTER (WHERE r.id IS NULL)
    FROM table_sessions ts
    LEFT JOIN restaurants r ON r.whatsapp_number = ts.bot_number
    UNION ALL
    SELECT 'waiter_alerts', '0028',
           COUNT(*) FILTER (WHERE r.id IS NULL)
    FROM waiter_alerts wa
    LEFT JOIN restaurants r ON r.whatsapp_number = wa.bot_number
    UNION ALL
    SELECT 'nps_waiting', '0028',
           COUNT(*) FILTER (WHERE r.id IS NULL)
    FROM nps_waiting nw
    LEFT JOIN restaurants r ON r.whatsapp_number = nw.bot_number
)
SELECT
    migration,
    tbl,
    orphans,
    CASE WHEN orphans = 0 THEN 'OK — safe to migrate'
                          ELSE 'BLOCKED — resolve orphans first'
    END AS status
FROM checks
ORDER BY migration, tbl;
