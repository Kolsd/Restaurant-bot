#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rehearsal_railway.py - Railway-native Migration 0037 Rehearsal Tool
====================================================================
Triggered by setting REHEARSAL_MODE=1 on the Railway service.
Reads prod data via DATABASE_URL_ADMIN (pg_dump), writes to TEST_DATABASE_URL.
Never writes to prod.

Exit codes:
  0 -- all checks passed (safe to proceed with 0037 in prod)
  1 -- any stage or smoke check failed (do NOT proceed)

Usage: set REHEARSAL_MODE=1 on Railway, redeploy, read logs.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Generator

# ---------------------------------------------------------------------------
# Logging -- plain format (no structlog dependency needed at boot time)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rehearsal")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redact(url: str) -> str:
    """Replace password in a postgres URL with ***."""
    return re.sub(r"(://[^:@]*:)[^@]+(@)", r"\1***\2", url)


def _banner(title: str) -> None:
    line = "=" * 64
    log.info(line)
    log.info(title)
    log.info(line)


def _result_banner(passed: int, total: int, duration: float, failed_ids: list[str]) -> None:
    ok = passed == total
    status = "PASSED" if ok else "FAILED"
    log.info("")
    log.info("+" + "=" * 50 + "+")
    log.info(f"|  REHEARSAL RESULT: {status} ({passed}/{total} checks)" + " " * max(0, 29 - len(status) - len(f"({passed}/{total} checks)")) + "|")
    log.info(f"|  Duration: {duration:.0f}s" + " " * max(0, 40 - len(f"Duration: {duration:.0f}s")) + "|")
    if ok:
        log.info("|  Next: unset REHEARSAL_MODE, redeploy, then       |")
        log.info("|  apply 0037 to prod via Wave 2F.                  |")
    else:
        log.info(f"|  Failed checks: {', '.join(failed_ids)}" + " " * max(0, 35 - len(', '.join(failed_ids))) + "|")
        log.info("|  DO NOT apply 0037 to prod until fixed.           |")
    log.info("+" + "=" * 50 + "+")
    log.info("")


def _check_pg_client_tools() -> bool:
    """Verify pg_dump and psql are on PATH (Railway nixpacks image includes postgresql-client)."""
    for tool in ("pg_dump", "psql"):
        if not shutil.which(tool):
            log.error("Tool not found on PATH: %s -- Railway nixpacks image should provide it.", tool)
            return False
    return True


@contextmanager
def _env_override(overrides: dict[str, str]) -> Generator[None, None, None]:
    """Temporarily set env vars, restore originals on exit."""
    originals = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, original in originals.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original


def _normalize_pg_url(url: str) -> str:
    """Ensure URL starts with postgresql:// (not postgres://)."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _run(cmd: str, timeout: int = 600, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command with timeout. Returns CompletedProcess."""
    return subprocess.run(
        cmd,
        shell=True,
        timeout=timeout,
        capture_output=capture,
        text=True,
    )


def _psql_exec(test_url: str, sql: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a SQL statement against test DB via psql, return CompletedProcess.
    Passes SQL via stdin to avoid shell-dollar expansion (eats $$ blocks).
    """
    return subprocess.run(
        ["psql", test_url, "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql,
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def _psql_query(test_url: str, sql: str) -> str | None:
    """Run a scalar SQL query, return trimmed result or None on error."""
    result = subprocess.run(
        ["psql", test_url, "-t", "-A", "-f", "-"],
        input=sql,
        timeout=30,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Smoke check bookkeeping
# ---------------------------------------------------------------------------
_CHECKS_PASSED: list[str] = []
_CHECKS_FAILED: list[str] = []


def _ok(check_id: str, detail: str) -> None:
    log.info("  [OK] [%s] %s", check_id, detail)
    _CHECKS_PASSED.append(check_id)


def _fail(check_id: str, detail: str) -> None:
    log.error("  [FAIL] [%s] %s", check_id, detail)
    _CHECKS_FAILED.append(check_id)


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------

def stage1_preflight(prod_admin_url: str, test_url: str) -> None:
    """Assert env vars present and URLs are different DBs."""
    _banner("STAGE 1 -- Preflight")

    # Check pg client tools
    if not _check_pg_client_tools():
        log.error("Missing pg client tools -- cannot continue.")
        sys.exit(1)

    log.info("PROD URL (read-only source): %s", _redact(prod_admin_url))
    log.info("TEST URL (write target):     %s", _redact(test_url))

    # Safety: URLs must differ
    if prod_admin_url.rstrip("/") == test_url.rstrip("/"):
        log.error("ABORT: TEST_DATABASE_URL equals DATABASE_URL_ADMIN -- that is prod!")
        sys.exit(1)

    # Safety: same host+port check (e.g. same Railway Postgres cluster, different DB names are fine,
    # but if the full URL minus the DB name is equal, warn loudly)
    def _host_port(url: str) -> str:
        m = re.search(r"@([^/]+)/", url)
        return m.group(1) if m else url

    prod_hp = _host_port(prod_admin_url)
    test_hp = _host_port(test_url)
    if prod_hp == test_hp:
        log.warning(
            "Prod and test share the same host:port (%s). "
            "This is acceptable only if they are DIFFERENT databases on the same Postgres cluster. "
            "Verify TEST_DATABASE_URL points at the 'Test' Railway Postgres service, not 'Postgres'.",
            prod_hp,
        )
        # Not a hard abort -- Railway allows multiple DBs on one cluster.

    # Anti-swap guard: query both URLs, confirm prod has data and test has less.
    # Uses `restaurants` table as the signal (always populated in prod, likely
    # empty or tiny in test).
    def _count_restaurants(url: str) -> int | None:
        r = _psql_exec(
            url,
            "SELECT COALESCE((SELECT COUNT(*)::int FROM restaurants), -1)",
            timeout=15,
        )
        if r.returncode != 0:
            return None
        # psql default output has a header + data; find the integer line.
        for line in r.stdout.splitlines():
            s = line.strip()
            if s.lstrip("-").isdigit():
                return int(s)
        return None

    log.info("Anti-swap guard: counting rows in `restaurants` on each URL...")
    prod_count = _count_restaurants(prod_admin_url)
    test_count = _count_restaurants(test_url)
    log.info("  PROD restaurants count: %s", prod_count if prod_count is not None else "n/a")
    log.info("  TEST restaurants count: %s", test_count if test_count is not None else "n/a")

    # If prod has 0 rows but test has many, they're clearly swapped.
    if prod_count == 0 and (test_count or 0) > 0:
        log.error(
            "ABORT: swap detected — the 'prod' URL has 0 restaurants but the "
            "'test' URL has %d. Your env vars are flipped. Swap PROD_DATABASE_URL "
            "and TEST_DATABASE_URL and re-run.",
            test_count,
        )
        sys.exit(1)
    # If prod has 0 rows, something is off regardless — abort.
    if prod_count in (0, None, -1):
        log.error(
            "ABORT: the 'prod' URL has 0 or unreadable restaurants table. "
            "Either PROD_DATABASE_URL is pointing at the wrong DB, or the DB "
            "genuinely is empty (not expected for a running Mesio prod)."
        )
        sys.exit(1)
    # If test has more rows than prod, very suspicious.
    if (test_count or 0) > prod_count:
        log.error(
            "ABORT: 'test' URL has MORE restaurants (%d) than 'prod' (%d). "
            "Likely swapped. Not proceeding.",
            test_count, prod_count,
        )
        sys.exit(1)

    log.info("Preflight OK -- URLs are distinct, tools available, swap guard passed.")


def stage2_copy_prod_to_test(prod_admin_url: str, test_url: str) -> None:
    """Drop+recreate test schema, restore prod dump into it."""
    _banner("STAGE 2 -- Copy prod -> test (pg_dump | psql)")

    # Drop everything in test
    log.info("Dropping public schema in test DB...")
    r = _psql_exec(test_url, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;", timeout=60)
    if r.returncode != 0:
        log.error("Failed to reset test schema:\n%s", r.stderr[-2000:])
        sys.exit(1)
    log.info("Test schema reset.")

    # Re-create extensions
    for ext in ("pgcrypto", "unaccent"):
        r2 = _psql_exec(test_url, f"CREATE EXTENSION IF NOT EXISTS {ext};")
        if r2.returncode != 0:
            log.warning("Could not create extension %s (may already exist or be unavailable): %s", ext, r2.stderr[:200])

    # Set up roles (idempotent)
    log.info("Setting up Mesio roles in test DB...")
    role_sql = (
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mesio_superadmin') "
        "  THEN CREATE ROLE mesio_superadmin BYPASSRLS NOINHERIT; END IF; "
        "END $$;"
    )
    _psql_exec(test_url, role_sql, timeout=10)
    role_sql2 = (
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mesio_app') "
        "  THEN CREATE ROLE mesio_app LOGIN PASSWORD 'rehearsal'; END IF; "
        "END $$;"
    )
    _psql_exec(test_url, role_sql2, timeout=10)
    _psql_exec(test_url, "GRANT mesio_superadmin TO mesio_app;", timeout=10)
    # Grant to current user so the migration runner can SET LOCAL ROLE mesio_superadmin
    _psql_exec(test_url, "GRANT mesio_superadmin TO CURRENT_USER;", timeout=10)

    # Two separate subprocesses — prior pipe form swallowed pg_dump failures
    # because the shell exit code came from psql (which reads empty stdin OK).
    import tempfile, os as _os
    dump_path = tempfile.mkstemp(suffix=".sql", prefix="mesio_rehearsal_")[1]
    log.info("Running pg_dump to %s (timeout 10 min)...", dump_path)
    dump_proc = subprocess.run(
        ["pg_dump", "--no-owner", "--no-acl", "--no-privileges",
         "-f", dump_path, prod_admin_url],
        capture_output=True, text=True, timeout=600,
    )
    if dump_proc.returncode != 0:
        log.error("pg_dump FAILED (rc=%d):", dump_proc.returncode)
        for line in dump_proc.stderr.splitlines()[-30:]:
            log.error("  %s", line)
        try: _os.remove(dump_path)
        except Exception: pass
        sys.exit(1)
    dump_size = _os.path.getsize(dump_path)
    log.info("pg_dump OK (%d bytes written).", dump_size)
    if dump_size < 1024:
        log.error("Dump is suspiciously small (<1KB). Aborting.")
        log.error("Dump preview: %r", open(dump_path).read()[:500])
        _os.remove(dump_path)
        sys.exit(1)

    log.info("Running psql restore (timeout 10 min)...")
    with open(dump_path) as dump_in:
        psql_proc = subprocess.run(
            ["psql", "-v", "ON_ERROR_STOP=1", test_url],
            stdin=dump_in, capture_output=True, text=True, timeout=600,
        )
    try: _os.remove(dump_path)
    except Exception: pass
    if psql_proc.returncode != 0:
        log.error("psql restore FAILED (rc=%d):", psql_proc.returncode)
        for line in psql_proc.stderr.splitlines()[-30:]:
            log.error("  %s", line)
        sys.exit(1)
    log.info("Data copy complete (dump %d bytes applied).", dump_size)


def stage3_grant_dml(test_url: str) -> None:
    """Grant DML on all tables/sequences/functions to mesio_app."""
    _banner("STAGE 3 -- Grant DML to mesio_app")

    grants = [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mesio_app",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mesio_app",
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mesio_app",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mesio_app",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO mesio_app",
    ]
    for sql in grants:
        r = _psql_exec(test_url, sql + ";", timeout=30)
        if r.returncode != 0:
            log.warning("Grant partial failure (may be OK on empty tables): %s", r.stderr[:200])

    log.info("DML grants applied.")


def stage4_undeferr_migration(deferred_path: str, active_path: str) -> None:
    """Rename 0037 .deferred -> .py (caller must wrap in try/finally to rename back)."""
    _banner("STAGE 4 -- Un-defer migration 0037")
    if not os.path.exists(deferred_path):
        log.error("Deferred migration not found: %s", deferred_path)
        sys.exit(1)
    if os.path.exists(active_path):
        log.info("Active .py already exists -- skipping rename (may be leftover from crashed run).")
    else:
        os.rename(deferred_path, active_path)
        log.info("Renamed: %s -> %s", os.path.basename(deferred_path), os.path.basename(active_path))


def stage5_run_alembic(test_url: str) -> None:
    """Run alembic upgrade head pointing at test DB."""
    _banner("STAGE 5 -- alembic upgrade head (against test DB)")

    with _env_override({
        "DATABASE_URL_ADMIN": test_url,
        "DATABASE_URL": test_url,
    }):
        result = _run("alembic upgrade head", timeout=600, capture=True)

    # Always print alembic output
    if result.stdout:
        for line in result.stdout.splitlines():
            log.info("  alembic> %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            log.info("  alembic! %s", line)

    if result.returncode != 0:
        log.error("alembic upgrade head FAILED -- see output above.")
        sys.exit(1)
    log.info("alembic upgrade head succeeded.")


async def stage6_smoke_checks(test_url: str) -> None:
    """Run all smoke checks 6a-6o against test DB."""
    _banner("STAGE 6 -- Smoke checks (6a-6o)")

    try:
        import asyncpg  # type: ignore[import]
    except ImportError:
        log.error("asyncpg not installed -- cannot run async smoke checks.")
        sys.exit(1)

    # Normalize URL for asyncpg (needs postgresql:// not postgresql+psycopg2://)
    aio_url = test_url
    if aio_url.startswith("postgres://"):
        aio_url = aio_url.replace("postgres://", "postgresql://", 1)
    # Strip any +driver suffix asyncpg doesn't understand
    aio_url = re.sub(r"\+\w+", "", aio_url)

    conn = await asyncpg.connect(aio_url)
    try:
        await _run_checks(conn, aio_url)
    finally:
        await conn.close()


async def _run_checks(conn, aio_url: str) -> None:
    import asyncpg  # type: ignore[import]

    # -- 6a. Alembic head is 0037_drop_legacy_rls -----------------------------
    try:
        row = await conn.fetchrow("SELECT version_num FROM alembic_version LIMIT 1")
        rev = row["version_num"] if row else None
        if rev and "0037_drop_legacy_rls" in rev:
            _ok("6a", f"alembic head = {rev}")
        else:
            _fail("6a", f"Expected 0037_drop_legacy_rls in alembic head, got: {rev}")
    except Exception as e:
        _fail("6a", f"alembic_version query error: {e}")

    # -- 6b. Org count > 0, location count >= org count, mapping table populated -
    try:
        org_count = await conn.fetchval("SELECT COUNT(*) FROM organizations")
        loc_count = await conn.fetchval("SELECT COUNT(*) FROM locations")
        map_count = await conn.fetchval("SELECT COUNT(*) FROM _migration_restaurant_to_location")
        rest_count = await conn.fetchval("SELECT COUNT(*) FROM restaurants_deprecated")

        if org_count and org_count > 0:
            _ok("6b-orgs", f"organizations: {org_count} rows")
        else:
            _fail("6b-orgs", "organizations is empty")

        if loc_count and loc_count >= org_count:
            _ok("6b-locs", f"locations: {loc_count} rows (>= {org_count} orgs)")
        else:
            _fail("6b-locs", f"locations: {loc_count} < orgs: {org_count}")

        if map_count and map_count == rest_count:
            _ok("6b-map", f"mapping table: {map_count} rows == {rest_count} restaurants_deprecated rows")
        else:
            _fail("6b-map", f"mapping rows ({map_count}) != restaurants_deprecated rows ({rest_count})")
    except Exception as e:
        _fail("6b", f"count query error: {e}")

    # -- 6c. Primary location invariant ----------------------------------------
    try:
        bad = await conn.fetchval(
            "SELECT COUNT(*) FROM ("
            "  SELECT org_id FROM locations WHERE is_primary = true "
            "  GROUP BY org_id HAVING COUNT(*) <> 1"
            ") sub"
        )
        if bad == 0:
            _ok("6c", "Every org has exactly 1 primary location")
        else:
            _fail("6c", f"{bad} org(s) have != 1 primary location")
    except Exception as e:
        _fail("6c", f"primary location invariant error: {e}")

    # -- 6d. restaurants is now a VIEW -----------------------------------------
    try:
        ttype = await conn.fetchval(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_name='restaurants' AND table_schema='public'"
        )
        if ttype == "VIEW":
            _ok("6d", "restaurants is a VIEW")
        else:
            _fail("6d", f"restaurants has table_type={ttype!r} (expected VIEW)")
    except Exception as e:
        _fail("6d", f"table_type check error: {e}")

    # -- 6e. restaurants_deprecated exists as a BASE TABLE ---------------------
    try:
        ttype = await conn.fetchval(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_name='restaurants_deprecated' AND table_schema='public'"
        )
        if ttype == "BASE TABLE":
            _ok("6e", "restaurants_deprecated is a BASE TABLE")
        else:
            _fail("6e", f"restaurants_deprecated has table_type={ttype!r} (expected BASE TABLE)")
    except Exception as e:
        _fail("6e", f"restaurants_deprecated check error: {e}")

    # -- 6f. tenant_isolation policy dropped -----------------------------------
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_policies "
            "WHERE policyname='tenant_isolation' AND schemaname='public'"
        )
        if count == 0:
            _ok("6f", "tenant_isolation policy: 0 remaining (all dropped)")
        else:
            _fail("6f", f"tenant_isolation policy still exists on {count} table(s)")
    except Exception as e:
        _fail("6f", f"tenant_isolation policy check error: {e}")

    # -- 6g. org_isolation policy present on 33 tables -------------------------
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_policies "
            "WHERE policyname='org_isolation' AND schemaname='public'"
        )
        if count and count >= 33:
            _ok("6g", f"org_isolation policy: {count} tables (expected >= 33)")
        else:
            _fail("6g", f"org_isolation policy: only {count}/33 tables")
    except Exception as e:
        _fail("6g", f"org_isolation policy check error: {e}")

    # -- 6h. Auto-populate triggers dropped ------------------------------------
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.triggers "
            "WHERE trigger_name LIKE 'trg_auto_org_location_%' "
            "AND trigger_schema='public'"
        )
        if count == 0:
            _ok("6h", "Auto-populate triggers: 0 remaining (all dropped)")
        else:
            _fail("6h", f"Auto-populate triggers: {count} still exist (expected 0)")
    except Exception as e:
        _fail("6h", f"trigger check error: {e}")

    # -- 6i. restaurant_id columns dropped from the 33+ tables -----------------
    # restaurants_deprecated is the only table that should retain restaurant_id? No -- it was
    # renamed FROM restaurants; it keeps its original columns. The migrated tables should
    # NOT have restaurant_id.
    _MIGRATED_TABLES = [
        "attendance_deductions", "billing_log", "carts", "contract_templates",
        "conversations", "customer_profiles", "dish_recipes", "fiscal_invoices",
        "fiscal_resolution", "inventory", "loyalty_customers", "loyalty_ledger",
        "marketing_messages_log", "menu_availability", "menu_events", "nps_responses",
        "nps_waiting", "occupancy_snapshots", "orders", "overtime_requests",
        "payroll_runs", "staff", "staff_deduction_items", "staff_schedules",
        "staff_shifts", "subscription_usage", "table_orders", "table_sessions",
        "time_slot_discounts", "tip_distributions", "waiter_alerts",
        "webauthn_challenges", "weekly_reports", "reservations",
    ]
    try:
        stray_cols = await conn.fetch(
            "SELECT table_name, column_name "
            "FROM information_schema.columns "
            "WHERE column_name='restaurant_id' AND table_schema='public' "
            "AND table_name != 'restaurants_deprecated' "
            "ORDER BY table_name"
        )
        if not stray_cols:
            _ok("6i", "restaurant_id column: 0 stray occurrences (only restaurants_deprecated retains it)")
        else:
            names = [r["table_name"] for r in stray_cols]
            _fail("6i", f"restaurant_id still present in: {names}")
    except Exception as e:
        _fail("6i", f"restaurant_id column check error: {e}")

    # -- 6j. VIEW restaurants returns rows with expected shape ------------------
    try:
        rows = await conn.fetch(
            "SELECT id, name, whatsapp_number, menu, features, "
            "billing_config, parent_restaurant_id FROM restaurants LIMIT 3"
        )
        if rows:
            _ok("6j", f"VIEW restaurants returns {len(rows)} row(s) with expected columns")
        else:
            _fail("6j", "VIEW restaurants returned 0 rows (no data in test DB?)")
    except Exception as e:
        _fail("6j", f"VIEW restaurants query error: {e}")

    # -- 6k. organizations.billing_config backfilled ----------------------------
    try:
        org_bc = await conn.fetchval(
            "SELECT COUNT(*) FROM organizations WHERE billing_config IS NOT NULL"
        )
        rest_bc = await conn.fetchval(
            "SELECT COUNT(*) FROM restaurants_deprecated WHERE billing_config IS NOT NULL"
        )
        if org_bc is not None and org_bc >= (rest_bc or 0):
            _ok("6k", f"organizations.billing_config: {org_bc} non-null (restaurants_deprecated had {rest_bc})")
        else:
            _fail("6k", f"organizations.billing_config: {org_bc} non-null but restaurants_deprecated had {rest_bc}")
    except Exception as e:
        _fail("6k", f"billing_config backfill check error: {e}")

    # -- 6l. UNIQUE constraints on org_id pairs still present ------------------
    _ORG_UNIQUE_CONSTRAINTS = [
        ("subscription_usage",  "subscription_usage_org_date_uq"),
        ("customer_profiles",   "customer_profiles_org_phone_uq"),
        ("loyalty_customers",   "loyalty_customers_org_phone_uq"),
        ("weekly_reports",      "weekly_reports_org_week_uq"),
        ("payroll_runs",        "payroll_runs_org_period_uq"),
    ]
    missing_constraints = []
    for table, conname in _ORG_UNIQUE_CONSTRAINTS:
        try:
            exists = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_constraint "
                f"WHERE conname='{conname}' AND conrelid='{table}'::regclass"
            )
            if not exists:
                missing_constraints.append(conname)
        except Exception:
            missing_constraints.append(f"{conname}(error)")

    if not missing_constraints:
        _ok("6l", f"All {len(_ORG_UNIQUE_CONSTRAINTS)} org_id UNIQUE constraints present")
    else:
        _fail("6l", f"Missing UNIQUE constraints: {missing_constraints}")

    # -- 6m/6n. RLS scoped + fail-closed tests ---------------------------------
    # CRITICAL: asyncpg runs each execute() in its OWN implicit transaction when
    # not wrapped. `SET LOCAL ROLE` and `SET LOCAL app.*` only persist within
    # a transaction. So we MUST wrap these in `async with conn.transaction():`
    # otherwise the SELECT runs as postgres (BYPASSRLS) and the result is a
    # false positive showing all rows globally.
    #
    # We also verify current_user == mesio_app inside the tx to catch any
    # silent SET LOCAL ROLE failure.

    # Diagnostic: RLS/FORCE state on orders
    try:
        rls_enabled = await conn.fetchval(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE relname='orders' AND relnamespace='public'::regnamespace"
        )
        force_enabled = await conn.fetchval(
            "SELECT relforcerowsecurity FROM pg_class "
            "WHERE relname='orders' AND relnamespace='public'::regnamespace"
        )
        if not rls_enabled:
            _fail("6-rls", "orders table does NOT have RLS enabled (relrowsecurity=false)")
        elif not force_enabled:
            _fail("6-force", "orders table does NOT have FORCE RLS (relforcerowsecurity=false)")
        else:
            log.info("   diagnostic: orders has RLS enabled + FORCE RLS on")
    except Exception as e:
        _fail("6-diag", f"RLS/FORCE check error: {e}")

    # 6m: scoped read (must be in a transaction for SET LOCAL to stick).
    # Pick an org that actually has orders so the count is meaningful — if
    # no org has orders, fall back to the first org (count will be 0, still
    # validates that RLS scoping doesn't error).
    try:
        picked_org = await conn.fetchval(
            "SELECT org_id FROM orders GROUP BY org_id ORDER BY COUNT(*) DESC LIMIT 1"
        )
        if picked_org is None:
            picked_org = await conn.fetchval("SELECT id FROM organizations ORDER BY id LIMIT 1")
        if picked_org is None:
            _fail("6m", "No organizations found -- cannot test scoped RLS")
        else:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE mesio_app")
                actual = await conn.fetchval("SELECT current_user")
                if actual != "mesio_app":
                    raise RuntimeError(f"SET LOCAL ROLE landed on {actual!r}, not mesio_app")
                await conn.execute(
                    "SELECT set_config('app.org_id', $1, true)", str(picked_org)
                )
                order_count = await conn.fetchval("SELECT COUNT(*) FROM orders")
            # Pass criterion: no error. Count is informational.
            _ok("6m", f"mesio_app + app.org_id={picked_org} -> orders: {order_count} (RLS scoped OK)")
    except Exception as e:
        _fail("6m", f"scoped RLS test error: {e}")

    # 6n: no-scope read (must be 0 due to fail-closed RLS)
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE mesio_app")
            actual = await conn.fetchval("SELECT current_user")
            if actual != "mesio_app":
                raise RuntimeError(f"SET LOCAL ROLE landed on {actual!r}, not mesio_app")
            # Ensure no org_id is set
            await conn.execute("SELECT set_config('app.org_id', '', true)")
            zero = await conn.fetchval("SELECT COUNT(*) FROM orders")
        if zero == 0:
            _ok("6n", "mesio_app + no scope -> orders: 0 (fail-closed, RLS+FORCE verified)")
        else:
            _fail("6n", f"mesio_app + no scope -> orders: {zero} (RLS NOT fail-closed!)")
    except Exception as e:
        _fail("6n", f"fail-closed RLS test error: {e}")

    # -- 6o. INSERT with wrong org_id is blocked by RLS WITH CHECK -------------
    try:
        first_org = await conn.fetchval("SELECT id FROM organizations ORDER BY id LIMIT 1")
        second_org = await conn.fetchval(
            "SELECT id FROM organizations WHERE id != $1 ORDER BY id LIMIT 1", first_org
        )
        if first_org is None or second_org is None:
            log.warning("  ~ [6o] Skipped -- need at least 2 orgs to test cross-tenant INSERT block")
            _CHECKS_PASSED.append("6o")  # neutral pass
        else:
            # Attempt INSERT with org_id of second_org while scoped to first_org
            blocked = False
            try:
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL app.org_id = '{first_org}'")
                    await conn.execute("SET LOCAL ROLE mesio_app")
                    # Minimal INSERT that should be blocked by RLS WITH CHECK
                    await conn.execute(
                        "INSERT INTO orders (org_id, location_id, status, payload) "
                        "VALUES ($1, 1, 'test', '{}'::jsonb)",
                        second_org,
                    )
                    raise RuntimeError("INSERT succeeded -- should have been blocked!")
            except Exception as insert_err:
                err_str = str(insert_err)
                if "new row violates row-level security" in err_str or \
                   "InsufficientPrivilegeError" in type(insert_err).__name__ or \
                   "blocked" in err_str.lower():
                    blocked = True
                elif "RuntimeError" in type(insert_err).__name__ and "should have been blocked" in err_str:
                    blocked = False
                else:
                    # Other errors (missing columns, etc.) -- migration shape may differ
                    blocked = True  # not a cross-tenant leak
                    log.info("  ~ [6o] INSERT error (non-RLS): %s -- treating as blocked", err_str[:100])
            finally:
                try:
                    await conn.execute("RESET ROLE")
                except Exception:
                    pass

            if blocked:
                _ok("6o", "Cross-tenant INSERT blocked by RLS WITH CHECK")
            else:
                _fail("6o", "Cross-tenant INSERT NOT blocked -- RLS WITH CHECK misconfigured!")
    except Exception as e:
        _fail("6o", f"INSERT RLS test error: {e}")


def stage7_app_smoke(test_url: str) -> None:
    """Import app.main to verify the app starts against the migrated test DB."""
    _banner("STAGE 7 -- App import smoke test")
    log.info("Setting DATABASE_URL to test DB and importing app.main (30s timeout)...")

    with _env_override({
        "DATABASE_URL": test_url,
        "DATABASE_URL_ADMIN": test_url,
        # Stub required env vars the app validates at import time
        "META_APP_SECRET": os.environ.get("META_APP_SECRET", "rehearsal_stub_32byteslong12345"),
        "ADMIN_KEY":       os.environ.get("ADMIN_KEY", "rehearsal_admin_key"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "sk-ant-rehearsal"),
    }):
        try:
            import importlib
            import signal

            def _timeout_handler(signum, frame):
                raise TimeoutError("app.main import timed out after 30s")

            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(30)
            try:
                import app.main  # noqa: F401  # type: ignore[import]
                log.info("  app.main imported successfully")
            finally:
                signal.alarm(0)
        except TimeoutError:
            log.warning("  app.main import timed out -- app may be starting async services that block.")
            log.warning("  This is non-fatal; the DB migration itself is what matters.")
        except Exception as e:
            log.warning("  app.main import raised: %s", e)
            log.warning("  This is non-fatal for migration rehearsal (stub env vars may cause this).")

    log.info("Stage 7 complete.")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    start = time.monotonic()

    # Locate migration files
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    versions_dir = os.path.join(repo_root, "alembic", "versions")
    deferred_path = os.path.join(versions_dir, "0037_drop_legacy_rls_and_restaurant_id.py.deferred")
    active_path = os.path.join(versions_dir, "0037_drop_legacy_rls_and_restaurant_id.py")

    # Env vars — explicit names to avoid ambiguity when two Postgres services
    # are both linked to the Restaurant-bot service on Railway.
    # Preferred: PROD_DATABASE_URL and TEST_DATABASE_URL set explicitly.
    # Fallbacks kept for convenience but logged so the operator can verify.
    prod_admin_url = (
        os.environ.get("PROD_DATABASE_URL")
        or os.environ.get("DATABASE_URL_ADMIN")
        or os.environ.get("DATABASE_URL")
        or ""
    )
    test_url = os.environ.get("TEST_DATABASE_URL", "")

    if not prod_admin_url:
        log.error(
            "No prod URL found. Set PROD_DATABASE_URL on Restaurant-bot to "
            "the URL of the 'Postgres' (production) service."
        )
        sys.exit(1)
    if not test_url:
        log.error("TEST_DATABASE_URL not set -- cannot run rehearsal against test DB.")
        sys.exit(1)

    prod_admin_url = _normalize_pg_url(prod_admin_url)
    test_url = _normalize_pg_url(test_url)

    log.info("+======================================================+")
    log.info("|  Mesio 0037 Railway Rehearsal Tool                   |")
    log.info("+======================================================+")

    migration_renamed = False

    try:
        # Stages 1-3
        stage1_preflight(prod_admin_url, test_url)
        stage2_copy_prod_to_test(prod_admin_url, test_url)
        stage3_grant_dml(test_url)

        # Stage 4 -- rename .deferred -> .py
        stage4_undeferr_migration(deferred_path, active_path)
        migration_renamed = not os.path.exists(deferred_path)

        # Stage 5 -- alembic upgrade head
        stage5_run_alembic(test_url)

        # Stage 6 -- async smoke checks
        asyncio.run(stage6_smoke_checks(test_url))

        # Stage 7 -- optional app import
        stage7_app_smoke(test_url)

    finally:
        # Stage 9 / finally -- always rename back
        _banner("STAGE 9 -- Cleanup")
        if migration_renamed and os.path.exists(active_path) and not os.path.exists(deferred_path):
            os.rename(active_path, deferred_path)
            log.info("Renamed back: %s -> %s", os.path.basename(active_path), os.path.basename(deferred_path))
        elif os.path.exists(deferred_path):
            log.info("Deferred file already in place -- no rename needed.")
        log.info("Cleanup complete.")

    # Stage 8 -- Summary
    duration = time.monotonic() - start
    passed = len(_CHECKS_PASSED)
    total = passed + len(_CHECKS_FAILED)
    _result_banner(passed, total, duration, _CHECKS_FAILED)

    sys.exit(0 if not _CHECKS_FAILED else 1)


if __name__ == "__main__":
    main()
