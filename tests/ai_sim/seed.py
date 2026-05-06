"""
tests/ai_sim/seed.py — Deterministic DB seeding for the AI simulation harness.

Provides:
  seed_restaurant(conn)       → seeds one test restaurant + tables + user + subscription
  truncate_test_data(conn)    → truncates volatile tables, re-seeds subscription_usage
  reset_state_store_fallbacks() → clears all in-process fallback dicts in state_store
"""
from __future__ import annotations

import json
import sys
import os

import asyncpg

# ── Re-use constants from setup_demo.py ───────────────────────────────────────
# sys.path adjustment so this works whether run from repo root or tests/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.setup_demo import DEMO_MENU, DEMO_FEATURES  # noqa: E402
from app.services.database import _normalize_phone  # noqa: E402
from app.services.logging import get_logger  # noqa: E402

log = get_logger(__name__)

# ── Sim-specific constants ────────────────────────────────────────────────────
# Scenarios pass this as bot_number to agent.chat(). The agent normalizes it via
# _normalize_phone (strips '+' and spaces) before looking up the restaurant. We
# MUST store the normalized form in DB or the lookup fails. "+57TESTBOT1" →
# "57TESTBOT1" in DB. Matches the invariant used by the rest of the codebase.
SIM_BOT_NUMBER_RAW = "+57TESTBOT1"                       # what scenarios pass in
SIM_BOT_NUMBER = _normalize_phone(SIM_BOT_NUMBER_RAW)    # what's stored in DB
SIM_RESTAURANT_NAME = "Mesio Test Restaurant"
SIM_ADDRESS = "Calle 93 #13-24, Bogotá (Sim)"
SIM_USER_EMAIL = "sim@mesio.co"

# Build features dict: start from DEMO_FEATURES, ensure all required keys
SIM_FEATURES = {
    **DEMO_FEATURES,
    "bot_active": True,
    "domicilio_active": True,
    "recoger_active": True,
    "currency": "COP",
    "locale": "es-CO",
    "timezone": "America/Bogota",
}

# Number of test tables to seed
SIM_TABLE_COUNT = 6


async def seed_restaurant(conn: asyncpg.Connection) -> dict:
    """Seed the test restaurant and supporting rows.

    Idempotent: uses ON CONFLICT DO NOTHING / pre-checks so it is safe to call
    multiple times.

    Returns:
        {"restaurant_id": int, "bot_number": str}
    """
    # ── 0. Clean orphaned sim data from prior runs ─────────────────────────────
    # If a previous seed created a restaurant and tables, then the restaurant
    # was deleted (manually or via failed run), the tables still point at the
    # old restaurant_id/branch_id. On re-seed we'd get a new restaurant id but
    # tables with stale branch_id → detect_table_context returns None → bot
    # thinks mesa mode is unavailable. Proactively purge orphan sim tables.
    await conn.execute(
        """
        DELETE FROM restaurant_tables
        WHERE id LIKE 'sim_mesa_%'
          AND branch_id NOT IN (SELECT id FROM restaurants WHERE whatsapp_number = $1)
        """,
        SIM_BOT_NUMBER,
    )

    # ── 1. Restaurant (post-0037: insert Organization + primary Location) ──────
    # The `restaurants` table is now a read-only VIEW over organizations+locations.
    # We seed the Org (tenant) and its primary Location.
    #
    # NOTE on identifiers:
    #   restaurant_id returned by this seed == org_id (the tenant key). This
    #   matches the runtime code which uses org_id as the tenant scope.
    #   location_id is the primary Location for operational scoping.
    existing_org = await conn.fetchrow(
        "SELECT id FROM organizations WHERE whatsapp_number = $1",
        SIM_BOT_NUMBER,
    )

    if existing_org:
        org_id: int = existing_org["id"]
        # Post-0038: locations.is_primary was dropped (every location is a peer).
        # The seed creates exactly one location per org, so just take any one
        # — the first by id is deterministic and stable.
        loc_id = await conn.fetchval(
            "SELECT id FROM locations WHERE org_id = $1 ORDER BY id ASC LIMIT 1",
            org_id,
        )
        log.info("seed.restaurant_exists", org_id=org_id, location_id=loc_id)
    else:
        # UNIQUE index on organizations.whatsapp_number is PARTIAL
        # (WHERE whatsapp_number IS NOT NULL in 0034), so ON CONFLICT can't
        # use it without specifying the predicate. Plain INSERT — existence
        # check above already prevented dupes.
        org_id = await conn.fetchval(
            """
            INSERT INTO organizations (name, whatsapp_number, menu, features, slug)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5)
            RETURNING id
            """,
            SIM_RESTAURANT_NAME,
            SIM_BOT_NUMBER,
            json.dumps(DEMO_MENU),
            json.dumps(SIM_FEATURES),
            "mesio-test-sim",
        )
        # Post-0041: no is_primary column, legacy_restaurant_id column dropped.
        loc_id = await conn.fetchval(
            """
            INSERT INTO locations (
                org_id, name, code, address, active, timezone
            )
            VALUES ($1, $2, 'principal', $3, true, 'America/Bogota')
            RETURNING id
            """,
            org_id, SIM_RESTAURANT_NAME, SIM_ADDRESS,
        )
        log.info("seed.restaurant_created", org_id=org_id, location_id=loc_id)

    # For legacy compat downstream, restaurant_id IS the org_id (tenant key).
    restaurant_id = org_id

    # ── 2. Owner user ──────────────────────────────────────────────────────────
    existing_user = await conn.fetchrow(
        "SELECT username FROM users WHERE username = $1",
        SIM_USER_EMAIL,
    )
    if not existing_user:
        # Use a simple placeholder hash (bcrypt of "sim2024") — not used for auth
        try:
            await conn.execute(
                """
                INSERT INTO users (username, password_hash, restaurant_name, role, branch_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (username) DO NOTHING
                """,
                SIM_USER_EMAIL,
                "$2b$12$placeholderhashneverusedXXXXXXXXXXXXXXX",
                SIM_RESTAURANT_NAME,
                "owner",
                restaurant_id,
            )
            log.info("seed.user_created", username=SIM_USER_EMAIL)
        except asyncpg.UniqueViolationError:
            log.info("seed.user_exists_race", username=SIM_USER_EMAIL)
    else:
        log.info("seed.user_exists", username=SIM_USER_EMAIL)

    # ── 3. Restaurant tables (mesas 1–6) ───────────────────────────────────────
    # restaurant_tables schema (0001): id TEXT PK, number INT, name TEXT,
    # branch_id INT, active BOOL, created_at TIMESTAMP
    for n in range(1, SIM_TABLE_COUNT + 1):
        table_id = f"sim_mesa_{n}"
        existing_table = await conn.fetchrow(
            "SELECT id FROM restaurant_tables WHERE id = $1",
            table_id,
        )
        if not existing_table:
            await conn.execute(
                """
                INSERT INTO restaurant_tables (
                    id, number, name, branch_id, active, org_id, location_id
                )
                VALUES ($1, $2, $3, $4, TRUE, $5, $6)
                ON CONFLICT (id) DO NOTHING
                """,
                table_id,
                n,
                f"Mesa {n}",
                loc_id,    # branch_id = the primary location id (legacy fk)
                org_id,    # org_id for RLS + tenant scope
                loc_id,    # location_id for operational routing
            )

    log.info("seed.tables_seeded", count=SIM_TABLE_COUNT)

    # ── 4. subscription_usage for current month (post-0037: use org_id) ────────
    # RLS WITH CHECK on subscription_usage rejects INSERTs as mesio_app without
    # app.org_id GUC. Use mesio_superadmin (BYPASSRLS) for cross-tenant seed.
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE mesio_superadmin")
        await conn.execute(
            """
            INSERT INTO subscription_usage (org_id, usage_date, total_tokens, total_invoices)
            VALUES ($1, CURRENT_DATE, 0, 0)
            ON CONFLICT (org_id, usage_date) DO NOTHING
            """,
            org_id,
        )
    log.info("seed.subscription_usage_seeded", org_id=org_id)

    return {"restaurant_id": restaurant_id, "bot_number": SIM_BOT_NUMBER}


# ── Volatile tables to truncate between scenarios ─────────────────────────────
# These are generated during a conversation and must be clean per-scenario.
# Preserves: restaurants, restaurant_tables, users (seed data).
_TRUNCATE_TABLES = [
    "conversations",
    "carts",
    "orders",
    "table_orders",
    "table_sessions",
    "table_checks",
    "waiter_alerts",
    "nps_responses",
    "nps_waiting",
    "reservations",
    "webhook_inbox",
    "subscription_usage",
]


async def truncate_test_data(conn: asyncpg.Connection) -> None:
    """Truncate all volatile tables and re-seed subscription_usage.

    NOTE: This does real cleanup — not a rollback trick. Required because
    agent.chat() opens its own pool connections and commits independently.

    Wave 2 / FORCE RLS note: TRUNCATE requires ownership or TRUNCATE privilege.
    If the sim runs as `mesio_app` (non-superuser, DML-only) TRUNCATE fails with
    `permission denied for table ...`. We fall back to DELETE FROM inside a
    transaction with `SET LOCAL ROLE mesio_superadmin` (BYPASSRLS) so DELETE is
    not filtered by org_isolation.
    """
    tables_sql = ", ".join(_TRUNCATE_TABLES)
    try:
        await conn.execute(f"TRUNCATE TABLE {tables_sql} CASCADE")
        log.info("seed.truncated_via_truncate", tables=_TRUNCATE_TABLES)
    except asyncpg.InsufficientPrivilegeError:
        # mesio_app lacks TRUNCATE on RLS-forced tables. Use DELETE with
        # BYPASSRLS role so we see (and delete) rows from every org.
        log.warning("seed.truncate_denied_falling_back_to_delete")
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE mesio_superadmin")
            # DELETE in reverse of _TRUNCATE_TABLES order so child tables
            # disappear before parents. _TRUNCATE_TABLES is authored with
            # parent-first ordering on the TRUNCATE CASCADE path, so reverse.
            for tbl in reversed(_TRUNCATE_TABLES):
                await conn.execute(f"DELETE FROM {tbl}")
        log.info("seed.truncated_via_delete", tables=_TRUNCATE_TABLES)

    # Re-seed subscription_usage so UsageLimitExceeded doesn't fire.
    # Post-0037: query organizations (not restaurants VIEW) and use org_id.
    org_id = await conn.fetchval(
        "SELECT id FROM organizations WHERE whatsapp_number = $1",
        SIM_BOT_NUMBER,
    )
    if org_id is not None:
        # Same RLS bypass as seed_restaurant — re-seed runs cross-tenant.
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE mesio_superadmin")
            await conn.execute(
                """
                INSERT INTO subscription_usage (org_id, usage_date, total_tokens, total_invoices)
                VALUES ($1, CURRENT_DATE, 0, 0)
                ON CONFLICT (org_id, usage_date) DO NOTHING
                """,
                org_id,
            )
        log.info("seed.subscription_usage_reseeded", org_id=org_id)


def reset_state_store_fallbacks() -> None:
    """Clear all in-process fallback dicts in app.services.state_store.

    This is required between scenarios so NPS / checkout / cart-lock state from
    scenario N does not bleed into scenario N+1 when running without Redis.

    Uses getattr defensively in case an attribute is renamed in a future refactor.
    """
    try:
        import app.services.state_store as ss  # noqa: PLC0415

        _dicts_to_clear = [
            "_fb_nps",
            "_fb_nps_done",
            "_fb_checkout",
            "_fb_cooldown",
            "_fb_cart_locks",
            "_fb_cart_lock_tokens",
            "_fb_rate_limits",
            "_fb_warn_last",
        ]
        for attr in _dicts_to_clear:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()

        log.info("seed.state_store_fallbacks_reset")
    except Exception as exc:  # noqa: BLE001
        log.warning("seed.state_store_reset_failed", error=str(exc))
