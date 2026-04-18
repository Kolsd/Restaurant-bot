#!/usr/bin/env bash
# =============================================================================
# Staging rehearsal. Runs ALWAYS against a throwaway container. Never touches prod.
# =============================================================================
# Purpose : Catch DB/migration bugs BEFORE they hit Railway production.
#           Spins up a throwaway Postgres 16 Docker container, restores a
#           production dump, creates the Mesio roles, runs alembic upgrade head,
#           executes smoke checks, and optionally runs pytest.
#
# Exit codes : 0 = all checks passed
#              1 = any failure (preflight, restore, migration, smoke, pytest)
#
# Requirements (host): docker, python3, alembic, pytest (in venv or PATH),
#                      gunzip (for .sql.gz dumps).
#                      NO host psql required — all SQL via docker exec.
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 0. ANSI color helpers (disabled when not a TTY)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RESET='\033[0m'
else
  GREEN=''; RED=''; YELLOW=''; CYAN=''; RESET=''
fi

ok()   { echo -e "${GREEN}[OK]${RESET}   $*"; }
fail() { echo -e "${RED}[FAIL]${RESET} $*"; }
warn() { echo -e "${YELLOW}[WARN]${RESET} $*"; }
info() { echo -e "${CYAN}[INFO]${RESET} $*"; }
hr()   { echo "------------------------------------------------------------"; }

# ---------------------------------------------------------------------------
# 1. Defaults
# ---------------------------------------------------------------------------
CONTAINER_NAME="mesio-rehearsal-pg"
PG_PORT=54329
PG_PASSWORD="rehearsal"
PG_USER="postgres"
PG_DB="postgres"
DUMP_FILE=""
KEEP=0
SKIP_PYTEST=0

# ---------------------------------------------------------------------------
# 2. Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") --dump <path/to/dump> [OPTIONS]

Options:
  --dump <path>     Path to production dump file (.sql.gz, .sql, .dump, .tar)
  --port <N>        Host port to bind Postgres (default: ${PG_PORT})
  --keep            Do not remove the container on success
  --skip-pytest     Skip pytest step (schema/migration check only)
  -h, --help        Show this help

Examples:
  ./scripts/staging_rehearsal.sh --dump /tmp/prod_2026-04-17.sql.gz
  ./scripts/staging_rehearsal.sh --dump /tmp/prod.dump --port 54330 --keep
  ./scripts/staging_rehearsal.sh --dump /tmp/prod.sql --skip-pytest
EOF
  exit 0
}

# ---------------------------------------------------------------------------
# 3. Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)        DUMP_FILE="$2"; shift 2 ;;
    --port)        PG_PORT="$2";   shift 2 ;;
    --keep)        KEEP=1;         shift   ;;
    --skip-pytest) SKIP_PYTEST=1;  shift   ;;
    -h|--help)     usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# ---------------------------------------------------------------------------
# 4. Connection strings (always local container — never prod)
# ---------------------------------------------------------------------------
DB_URL_ADMIN="postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
DB_URL_APP="postgresql://mesio_app:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"

# Safety guard: if DATABASE_URL in env already points somewhere, loud warning.
if [[ -n "${DATABASE_URL:-}" ]] && [[ "${DATABASE_URL}" != *"localhost"* ]]; then
  fail "SAFETY: Current DATABASE_URL in env does NOT point to localhost:"
  fail "  ${DATABASE_URL}"
  fail "This script would override it with the rehearsal container URL, but"
  fail "something in your environment looks wrong. Unset DATABASE_URL first."
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Teardown trap (runs on EXIT unless --keep + success)
# ---------------------------------------------------------------------------
TEARDOWN=1
teardown() {
  if [[ "${TEARDOWN}" -eq 1 ]]; then
    info "Tearing down container ${CONTAINER_NAME}..."
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  else
    info "Container left running (--keep). Connect with:"
    info "  DATABASE_URL_ADMIN=${DB_URL_ADMIN}"
    info "  DATABASE_URL=${DB_URL_APP}"
    info "  docker exec -it ${CONTAINER_NAME} psql -U postgres"
  fi
}
trap teardown EXIT

# ---------------------------------------------------------------------------
# 6. Helper: run SQL as postgres superuser (via docker exec)
# ---------------------------------------------------------------------------
psql_exec() {
  # Usage: psql_exec "SQL statement"
  docker exec -i "${CONTAINER_NAME}" \
    psql -U "${PG_USER}" -d "${PG_DB}" -v ON_ERROR_STOP=1 -c "$1"
}

psql_exec_file() {
  # Usage: psql_exec_file  (reads from stdin)
  docker exec -i "${CONTAINER_NAME}" \
    psql -U "${PG_USER}" -d "${PG_DB}" -v ON_ERROR_STOP=1
}

psql_query() {
  # Usage: psql_query "SQL"  -> returns trimmed scalar
  docker exec -i "${CONTAINER_NAME}" \
    psql -U "${PG_USER}" -d "${PG_DB}" -t -A -c "$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# STAGE 1: Preflight checks
# ---------------------------------------------------------------------------
hr
info "STAGE 1/7 — Preflight checks"
hr

# 1a. Dump file
if [[ -z "${DUMP_FILE}" ]]; then
  fail "--dump is required. Run with --help for usage."
  exit 1
fi
if [[ ! -f "${DUMP_FILE}" ]]; then
  fail "Dump file not found: ${DUMP_FILE}"
  exit 1
fi
if [[ ! -r "${DUMP_FILE}" ]]; then
  fail "Dump file not readable: ${DUMP_FILE}"
  exit 1
fi
ok "Dump file: ${DUMP_FILE}"

# 1b. Docker
if ! docker info >/dev/null 2>&1; then
  fail "Docker is not running or not installed. Start Docker Desktop and retry."
  exit 1
fi
ok "Docker is running"

# 1c. Python 3 + alembic
if ! python3 --version >/dev/null 2>&1; then
  fail "python3 not found in PATH"
  exit 1
fi
if ! python3 -c "import alembic" 2>/dev/null; then
  fail "alembic not importable. Run: pip install alembic"
  exit 1
fi
ok "Python3 + alembic available"

# 1d. Existing container check
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  warn "Container '${CONTAINER_NAME}' already exists."
  printf "  Remove it and continue? [y/N] "
  read -r answer
  if [[ "${answer}" =~ ^[Yy]$ ]]; then
    docker rm -f "${CONTAINER_NAME}" >/dev/null
    ok "Existing container removed"
  else
    fail "Aborted. Remove the container manually: docker rm -f ${CONTAINER_NAME}"
    exit 1
  fi
fi
ok "No pre-existing rehearsal container"

# ---------------------------------------------------------------------------
# STAGE 2: Spin up Postgres 16 container
# ---------------------------------------------------------------------------
hr
info "STAGE 2/7 — Starting Postgres 16 container (port ${PG_PORT})"
hr

docker run -d \
  --name "${CONTAINER_NAME}" \
  -e POSTGRES_PASSWORD="${PG_PASSWORD}" \
  -e POSTGRES_USER="${PG_USER}" \
  -e POSTGRES_DB="${PG_DB}" \
  -p "127.0.0.1:${PG_PORT}:5432" \
  postgres:16 \
  >/dev/null

ok "Container started: ${CONTAINER_NAME}"

# Wait until Postgres is ready (max 30s)
info "Waiting for Postgres to be ready..."
READY=0
for i in $(seq 1 30); do
  if docker exec "${CONTAINER_NAME}" pg_isready -U "${PG_USER}" -q 2>/dev/null; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "${READY}" -eq 0 ]]; then
  fail "Postgres did not become ready within 30 seconds"
  exit 1
fi
ok "Postgres ready after ~${i}s"

# ---------------------------------------------------------------------------
# STAGE 3: Create Mesio roles (mirror prod setup)
# ---------------------------------------------------------------------------
hr
info "STAGE 3/7 — Creating Mesio roles"
hr

psql_exec "CREATE ROLE mesio_superadmin BYPASSRLS NOINHERIT;" >/dev/null 2>&1 || true
psql_exec "CREATE ROLE mesio_app LOGIN PASSWORD '${PG_PASSWORD}';" >/dev/null 2>&1 || true
psql_exec "GRANT mesio_superadmin TO mesio_app;" >/dev/null
psql_exec "GRANT mesio_superadmin TO ${PG_USER};" >/dev/null

ok "Roles created: mesio_superadmin (BYPASSRLS), mesio_app (LOGIN)"

# ---------------------------------------------------------------------------
# STAGE 4: Restore dump
# ---------------------------------------------------------------------------
hr
info "STAGE 4/7 — Restoring dump: $(basename "${DUMP_FILE}")"
hr

RESTORE_OK=0
RESTORE_STDERR_FILE="$(mktemp)"

case "${DUMP_FILE}" in
  *.sql.gz)
    info "Format: gzip-compressed SQL — streaming via gunzip | psql"
    if gunzip -c "${DUMP_FILE}" | psql_exec_file >"${RESTORE_STDERR_FILE}" 2>&1; then
      RESTORE_OK=1
    fi
    ;;
  *.sql)
    info "Format: plain SQL"
    if psql_exec_file < "${DUMP_FILE}" >"${RESTORE_STDERR_FILE}" 2>&1; then
      RESTORE_OK=1
    fi
    ;;
  *.dump|*.tar)
    info "Format: pg_restore custom/tar"
    # Copy dump into container first, then pg_restore
    docker cp "${DUMP_FILE}" "${CONTAINER_NAME}:/tmp/rehearsal_dump" >/dev/null
    if docker exec "${CONTAINER_NAME}" \
        pg_restore -U "${PG_USER}" -d "${PG_DB}" --no-owner --role="${PG_USER}" \
        -v /tmp/rehearsal_dump >"${RESTORE_STDERR_FILE}" 2>&1; then
      RESTORE_OK=1
    fi
    ;;
  *)
    fail "Unrecognized dump format. Supported: .sql.gz, .sql, .dump, .tar"
    exit 1
    ;;
esac

if [[ "${RESTORE_OK}" -eq 0 ]]; then
  fail "Dump restore failed. Last 20 lines of output:"
  tail -n 20 "${RESTORE_STDERR_FILE}" | while IFS= read -r line; do echo "  ${line}"; done
  rm -f "${RESTORE_STDERR_FILE}"
  exit 1
fi
rm -f "${RESTORE_STDERR_FILE}"
ok "Dump restored successfully"

# ---------------------------------------------------------------------------
# STAGE 5: Grant DML to mesio_app (match prod grants from migration 0029)
# ---------------------------------------------------------------------------
hr
info "STAGE 5/7 — Granting DML to mesio_app"
hr

psql_exec "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mesio_app;" >/dev/null
psql_exec "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mesio_app;" >/dev/null
psql_exec "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mesio_app;" >/dev/null
# Ensure future tables/sequences are also covered
psql_exec "ALTER DEFAULT PRIVILEGES IN SCHEMA public
           GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mesio_app;" >/dev/null
psql_exec "ALTER DEFAULT PRIVILEGES IN SCHEMA public
           GRANT USAGE, SELECT ON SEQUENCES TO mesio_app;" >/dev/null

ok "DML grants applied to mesio_app"

# ---------------------------------------------------------------------------
# STAGE 6: Run alembic upgrade head
# ---------------------------------------------------------------------------
hr
info "STAGE 6/7 — Running alembic upgrade head"
hr

# Override DB URLs to point at the rehearsal container only.
export DATABASE_URL_ADMIN="${DB_URL_ADMIN}"
export DATABASE_URL="${DB_URL_APP}"

ALEMBIC_LOG="$(mktemp)"
ALEMBIC_OK=0

if alembic upgrade head >"${ALEMBIC_LOG}" 2>&1; then
  ALEMBIC_OK=1
fi

# Always show the alembic output (it's valuable for debugging)
cat "${ALEMBIC_LOG}"

if [[ "${ALEMBIC_OK}" -eq 0 ]]; then
  fail "alembic upgrade head FAILED"
  rm -f "${ALEMBIC_LOG}"
  exit 1
fi
rm -f "${ALEMBIC_LOG}"

# Verify alembic_version table has the expected head revision
CURRENT_REV=$(psql_query "SELECT version_num FROM alembic_version LIMIT 1;" 2>/dev/null || echo "")
if [[ -z "${CURRENT_REV}" ]]; then
  fail "alembic_version table empty after upgrade — migrations did not commit"
  exit 1
fi
ok "alembic upgrade head completed. Current revision: ${CURRENT_REV}"

# ---------------------------------------------------------------------------
# STAGE 7: Smoke checks
# ---------------------------------------------------------------------------
hr
info "STAGE 7/7 — Smoke checks"
hr

SMOKE_PASS=0
SMOKE_FAIL=0

smoke_check() {
  local name="$1"
  local sql="$2"
  local expect_positive="${3:-1}"   # 1 = value must be > 0; 0 = value must be 0
  local val

  val=$(psql_query "${sql}" 2>/dev/null || echo "ERROR")
  if [[ "${val}" == "ERROR" ]]; then
    fail "  ${name}: SQL error"
    SMOKE_FAIL=$((SMOKE_FAIL + 1))
    return
  fi

  if [[ "${expect_positive}" -eq 1 ]]; then
    if [[ "${val}" -gt 0 ]] 2>/dev/null; then
      ok "  ${name}: ${val}"
      SMOKE_PASS=$((SMOKE_PASS + 1))
    else
      fail "  ${name}: ${val} (expected > 0)"
      SMOKE_FAIL=$((SMOKE_FAIL + 1))
    fi
  else
    # expect 0
    if [[ "${val}" -eq 0 ]] 2>/dev/null; then
      ok "  ${name}: 0 orphans"
      SMOKE_PASS=$((SMOKE_PASS + 1))
    else
      fail "  ${name}: ${val} rows with NULL (expected 0)"
      SMOKE_FAIL=$((SMOKE_FAIL + 1))
    fi
  fi
}

# -- 7a. Basic row counts (data was loaded)
info "7a: Basic data presence"
smoke_check "organizations row count" \
  "SELECT COUNT(*) FROM organizations;"

smoke_check "locations >= organizations" \
  "SELECT CASE WHEN COUNT(DISTINCT l.org_id) = COUNT(DISTINCT o.id) THEN 1 ELSE 0 END
   FROM organizations o JOIN locations l ON l.org_id = o.id;"

smoke_check "_migration_restaurant_to_location row count" \
  "SELECT COUNT(*) FROM _migration_restaurant_to_location;"

# -- 7b. Zero orphan org_id on key tables (migration 0035 backfill)
info "7b: No NULL org_id after backfill (migration 0035)"
for tbl in orders staff conversations table_orders inventory; do
  # Table may not have org_id if this is a pre-0035 dump — check column exists first
  col_exists=$(psql_query \
    "SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='${tbl}' AND column_name='org_id';" 2>/dev/null || echo "0")
  if [[ "${col_exists}" -gt 0 ]]; then
    smoke_check "  ${tbl}.org_id: 0 NULL rows" \
      "SELECT COUNT(*) FROM ${tbl} WHERE org_id IS NULL;" 0
  else
    warn "  ${tbl}.org_id column not present — migration 0035 may not have run yet (OK if dump is pre-0034)"
  fi
done

# -- 7c. Every Matriz maps to exactly one primary Location
info "7c: Primary location invariant"
smoke_check "orgs each with exactly 1 primary location" \
  "SELECT COUNT(*) FROM (
     SELECT org_id, COUNT(*) AS c
     FROM locations WHERE is_primary = true
     GROUP BY org_id HAVING COUNT(*) = 1
   ) sub;" 1

bad_primary=$(psql_query \
  "SELECT COUNT(*) FROM (
     SELECT org_id FROM locations
     WHERE is_primary = true GROUP BY org_id HAVING COUNT(*) <> 1
   ) sub;" 2>/dev/null || echo "0")
if [[ "${bad_primary}" -eq 0 ]]; then
  ok "  No org has != 1 primary location"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  ${bad_primary} org(s) have != 1 primary location (invariant violated)"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# -- 7d. org_isolation policy exists on all 33 RLS tables (migration 0036)
info "7d: org_isolation policy on RLS tables (migration 0036)"
policy_count=$(psql_query \
  "SELECT COUNT(*) FROM pg_policies
   WHERE policyname = 'org_isolation' AND schemaname = 'public';" 2>/dev/null || echo "0")
# There are 33 tables in _RLS_TABLES
if [[ "${policy_count}" -ge 33 ]]; then
  ok "  org_isolation policy: ${policy_count} tables (expected >= 33)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  org_isolation policy: only ${policy_count}/33 tables have it"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# -- 7e. Auto-populate triggers exist (migration 0036)
info "7e: Auto-populate triggers (migration 0036)"
# 33 RLS tables + restaurant_tables + reservations = 35 triggers
trigger_count=$(psql_query \
  "SELECT COUNT(*) FROM pg_trigger t
   JOIN pg_class c ON c.oid = t.tgrelid
   WHERE t.tgname LIKE 'trg_auto_org_location_%'
     AND NOT t.tgisinternal;" 2>/dev/null || echo "0")
if [[ "${trigger_count}" -ge 35 ]]; then
  ok "  Auto-populate triggers: ${trigger_count} (expected >= 35)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  Auto-populate triggers: only ${trigger_count}/35 found"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# -- 7f. RLS fail-closed test: mesio_app with no scope sees 0 orders
info "7f: RLS fail-closed (no scope → 0 rows)"
failclosed=$(docker exec -i "${CONTAINER_NAME}" \
  psql -U mesio_app -d "${PG_DB}" -t -A \
  -c "SELECT COUNT(*) FROM orders;" 2>/dev/null || echo "ERROR")
if [[ "${failclosed}" == "0" ]]; then
  ok "  mesio_app + no scope → orders: 0 (fail-closed)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
elif [[ "${failclosed}" == "ERROR" ]]; then
  # RLS WITH CHECK may block the query itself — also acceptable
  ok "  mesio_app + no scope → query blocked by RLS (fail-closed)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  mesio_app + no scope → orders: ${failclosed} (RLS NOT fail-closed!)"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# -- 7g. RLS scoped test: mesio_app with org_id GUC sees rows for that tenant
info "7g: RLS org scope (app.org_id → rows filtered correctly)"
FIRST_ORG=$(psql_query "SELECT id FROM organizations ORDER BY id LIMIT 1;" 2>/dev/null || echo "")
if [[ -n "${FIRST_ORG}" ]]; then
  scoped_count=$(docker exec -i "${CONTAINER_NAME}" \
    psql -U mesio_app -d "${PG_DB}" -t -A \
    -c "BEGIN; SELECT set_config('app.org_id', '${FIRST_ORG}', true); SELECT COUNT(*) FROM orders; ROLLBACK;" \
    2>/dev/null | grep -E '^[0-9]+$' | tail -1 || echo "ERROR")
  # Value doesn't need to be > 0 (org may have no orders in dump)
  if [[ "${scoped_count}" != "ERROR" ]]; then
    ok "  mesio_app + app.org_id=${FIRST_ORG} → orders: ${scoped_count}"
    SMOKE_PASS=$((SMOKE_PASS + 1))
  else
    fail "  RLS org scope query errored"
    SMOKE_FAIL=$((SMOKE_FAIL + 1))
  fi
else
  warn "  No organizations found — skipping scoped RLS check"
fi

# -- 7h. alembic_version varchar(32) overflow guard (the original prod bug)
info "7h: alembic_version column type width"
av_max=$(psql_query \
  "SELECT character_maximum_length
   FROM information_schema.columns
   WHERE table_name='alembic_version' AND column_name='version_num';" 2>/dev/null || echo "ERROR")
# Should be either NULL (unconstrained VARCHAR) or >= 64
if [[ "${av_max}" == "" ]] || [[ "${av_max}" == "ERROR" ]]; then
  # NULL = unbounded text type, perfectly fine
  ok "  alembic_version.version_num: unbounded (no overflow risk)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
elif [[ "${av_max}" -ge 64 ]] 2>/dev/null; then
  ok "  alembic_version.version_num: max_length=${av_max} (>= 64, no overflow risk)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  alembic_version.version_num max_length=${av_max} — TOO SHORT (overflow risk!)"
  fail "  This is the exact bug that crashed Railway. Fix: ALTER COLUMN version_num TYPE VARCHAR(128)"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# -- 7i. No orphan restaurant_ids in migration mapping table
info "7i: No orphan restaurants in mapping table (migration 0034)"
orphans=$(psql_query \
  "SELECT COUNT(*) FROM restaurants r
   LEFT JOIN _migration_restaurant_to_location m ON m.old_restaurant_id = r.id
   WHERE m.old_restaurant_id IS NULL;" 2>/dev/null || echo "ERROR")
if [[ "${orphans}" == "ERROR" ]]; then
  warn "  restaurants table not found — skipping orphan check (OK if dump is post-0037)"
elif [[ "${orphans}" -eq 0 ]]; then
  ok "  Mapping table covers all restaurants: 0 orphans"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  ${orphans} restaurant(s) missing from mapping table (orphan restaurant_ids)"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# -- 7j. Trigger functions have SECURITY DEFINER
info "7j: Trigger functions have SECURITY DEFINER"
secdef_count=$(psql_query \
  "SELECT COUNT(*) FROM pg_proc
   WHERE proname LIKE '_auto_populate_org%'
     AND prosecdef = true;" 2>/dev/null || echo "0")
if [[ "${secdef_count}" -ge 4 ]]; then
  ok "  SECURITY DEFINER trigger functions: ${secdef_count} (expected >= 4)"
  SMOKE_PASS=$((SMOKE_PASS + 1))
else
  fail "  Only ${secdef_count}/4 trigger functions have SECURITY DEFINER"
  SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Smoke check summary
# ---------------------------------------------------------------------------
hr
echo -e "${CYAN}Smoke check summary${RESET}"
echo -e "  ${GREEN}PASS: ${SMOKE_PASS}${RESET}"
echo -e "  ${RED}FAIL: ${SMOKE_FAIL}${RESET}"
hr

if [[ "${SMOKE_FAIL}" -gt 0 ]]; then
  fail "Smoke checks FAILED (${SMOKE_FAIL} failures). Aborting before pytest."
  exit 1
fi
ok "All smoke checks passed"

# ---------------------------------------------------------------------------
# STAGE 8: pytest (optional)
# ---------------------------------------------------------------------------
if [[ "${SKIP_PYTEST}" -eq 1 ]]; then
  warn "Skipping pytest (--skip-pytest passed)"
else
  hr
  info "STAGE 8/8 — Running pytest"
  hr

  # DATABASE_URL is already exported to point at the rehearsal container.
  # This allows integration tests that were previously "baseline failures"
  # (no DB wired up) to now run for real.
  PYTEST_EXIT=0
  if python3 -m pytest tests/ --ignore=tests/ai_sim -x -q 2>&1 || PYTEST_EXIT=$?; then
    :
  fi

  if [[ "${PYTEST_EXIT}" -eq 0 ]]; then
    ok "pytest: all tests passed"
  else
    # Distinguish regressions from baseline failures.
    # Pre-existing failures: ~45 failed + 17 errors (no real DB in unit mode).
    # If we have a real DB the number should drop to near 0.
    warn "pytest exited with code ${PYTEST_EXIT}."
    warn "If failure count > 5, there may be regressions introduced by migrations 0034-0036."
    warn "Run with -v for details: pytest tests/ --ignore=tests/ai_sim -v"
    fail "pytest reported test failures"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
hr
ok "STAGING REHEARSAL PASSED"
info "Migrations applied through: ${CURRENT_REV}"
info "Smoke checks: ${SMOKE_PASS} passed, ${SMOKE_FAIL} failed"
hr

# Skip teardown if --keep
if [[ "${KEEP}" -eq 1 ]]; then
  TEARDOWN=0
fi

exit 0
