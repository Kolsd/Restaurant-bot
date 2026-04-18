# Staging Rehearsal Script

Catches DB/migration bugs BEFORE they hit Railway production.

## What it does

1. Spins up a throwaway Postgres 16 Docker container (port 54329 by default)
2. Creates the three Mesio roles (`postgres` superuser, `mesio_superadmin` BYPASSRLS, `mesio_app` non-superuser)
3. Restores a production dump (`.sql.gz`, `.sql`, `.dump`, or `.tar`)
4. Grants DML to `mesio_app` to match the prod role setup
5. Runs `alembic upgrade head` against the restored data
6. Executes 10 smoke checks (schema, RLS, trigger, data integrity)
7. Optionally runs pytest with the rehearsal DB wired up
8. Tears down the container on exit (unless `--keep`)

The script **always** overrides `DATABASE_URL` and `DATABASE_URL_ADMIN` to point
at `localhost:<port>` for the duration of the run. It refuses to proceed if
`DATABASE_URL` in your environment points anywhere other than localhost.

---

## Prerequisites

| Tool | Notes |
|------|-------|
| Docker Desktop | Must be running. `docker info` should succeed. |
| Python 3 + alembic | `pip install alembic` in your virtualenv. |
| pytest | `pip install pytest` — only needed if not using `--skip-pytest`. |
| gunzip | Bundled with Git Bash on Windows. Required for `.sql.gz` dumps only. |

No host-side `psql` is required — all SQL runs via `docker exec` into the container.

---

## How to get a production dump

Using the Railway CLI:

```bash
# Install Railway CLI once
npm install -g @railway/cli

# Dump (produces a .dump format file by default)
railway run pg_dump --format=custom --compress=9 \
  --no-owner --no-acl \
  -f /tmp/prod_$(date +%Y-%m-%d).dump
```

Or use pgAdmin / any Postgres tool pointed at your Railway DB URL.

---

## Running

```bash
# Basic run — migrations + smoke checks + pytest
./scripts/staging_rehearsal.sh --dump /tmp/prod_2026-04-17.sql.gz

# Skip pytest (schema/migration check only)
./scripts/staging_rehearsal.sh --dump /tmp/prod.dump --skip-pytest

# Keep the container after success (inspect manually)
./scripts/staging_rehearsal.sh --dump /tmp/prod.sql --keep

# Custom port (if 54329 is in use)
./scripts/staging_rehearsal.sh --dump /tmp/prod.dump --port 54330

# Run twice in a row cleanly (first run tears down the container on exit)
./scripts/staging_rehearsal.sh --dump /tmp/prod.dump
./scripts/staging_rehearsal.sh --dump /tmp/prod.dump   # works fine
```

Run from the **repo root**:

```bash
cd /path/to/Restaurant-bot
bash scripts/staging_rehearsal.sh --dump /tmp/prod.dump
```

---

## What each smoke check catches

| Check | What it catches |
|-------|----------------|
| **7a — Basic row counts** | Dump restore failed silently; organizations/locations empty after migration 0034 backfill |
| **7b — Zero NULL org_id** | Migration 0035 backfill incomplete; orphan rows that slip past RLS fail-closed and become invisible to the app |
| **7c — Primary location invariant** | Branch-to-location mapping produced duplicate primaries; the `UNIQUE PARTIAL INDEX` not enforced (the "branch_id misclassification" class of bug) |
| **7d — org_isolation policy count** | Migration 0036 policy creation loop stopped early; fewer than 33 tables protected |
| **7e — Auto-populate trigger count** | Migration 0036 trigger loop failed; old app code (INSERT with only `restaurant_id`) would break WITH CHECK on the new policy |
| **7f — RLS fail-closed** | `mesio_app` with no scope returns 0 rows (not all rows). Catches accidental BYPASSRLS grant or missing ENABLE RLS |
| **7g — RLS org scope** | `SET app.org_id` + query works end-to-end for the new policy |
| **7h — alembic_version column width** | `VARCHAR(32)` overflow crash on Railway (the original incident trigger). Revision IDs like `0034_org_locations` are 20 chars — safe, but this guard ensures no future migration blows past 32 |
| **7i — No orphan restaurants** | Migration 0034 mapping table missed some restaurants; those tenants lose all their data under the new RLS scope |
| **7j — SECURITY DEFINER on trigger functions** | If functions lack SECURITY DEFINER, `mesio_app` can't read the mapping table during INSERT — triggers silently fail to populate `org_id`, causing WITH CHECK violations on every write |

---

## Recommended workflow (before merging a migration PR)

1. **Take a fresh dump** from Railway before starting:
   ```bash
   railway run pg_dump --format=custom -f /tmp/prod_$(date +%F).dump
   ```

2. **Run the rehearsal locally:**
   ```bash
   ./scripts/staging_rehearsal.sh --dump /tmp/prod_$(date +%F).dump
   ```

3. **Fix any failures** in the migration script, then re-run. The script tears
   down and recreates the container each run so reruns are clean.

4. **Pass the output** to your reviewer as proof the migration ran cleanly
   against real production data.

5. **Only then** deploy to Railway (`git push` → Railway runs
   `alembic upgrade head && uvicorn ...`).

---

## Troubleshooting

### "Docker is not running"
Start Docker Desktop. On Windows 11, look for the whale icon in the system tray.

### "Container 'mesio-rehearsal-pg' already exists"
A previous run left the container. The script offers to remove it interactively.
Or remove it manually:
```bash
docker rm -f mesio-rehearsal-pg
```

### "Port 54329 already in use"
Pass `--port <N>` with a free port:
```bash
./scripts/staging_rehearsal.sh --dump /tmp/prod.dump --port 54331
```
Find a free port: `netstat -an | grep 5433`

### "Dump restore failed"
The last 20 lines of restore output are printed. Common causes:
- **Wrong format flag** — `.dump` files need `pg_restore`; `.sql` files need `psql`. The script auto-detects by extension. If your file has an unusual extension, rename it.
- **Role not found during restore** — errors like `role "myuser" does not exist`. These are usually warnings, not fatal. If the restore aborts, try adding `--no-owner --no-acl` flags to your `pg_dump` command when creating the dump.
- **Encoding mismatch** — usually safe to ignore. If it aborts, create the dump with `--encoding=UTF8`.

### "alembic upgrade head FAILED"
The full alembic output is printed above the FAIL line. Common causes:
- **SQL syntax error** — the migration has a bug in an `op.execute()` call.
- **Column already exists** — migration is not idempotent. Add `IF NOT EXISTS`.
- **Role 'mesio_app' does not exist** — Stage 3 failed silently. Run with `set -x` to debug: `bash -x scripts/staging_rehearsal.sh ...`
- **psycopg2 not installed** — `pip install psycopg2-binary`

### Smoke check 7h fails (alembic_version VARCHAR width)
Your database still has the old narrow column. Fix in a migration:
```sql
ALTER TABLE alembic_version
    ALTER COLUMN version_num TYPE VARCHAR(128);
```
Or simply widen it in the next migration with `op.execute(sa.text(...))`.

### pytest fails with many errors
With a real DB wired, previously-skipped integration tests now run. Expected:
- Pre-migration baseline: ~45 failed + 17 errors (no DB in CI unit mode)
- Post-rehearsal with real DB: should approach 0 failures

If failure count is large, run verbosely to identify the pattern:
```bash
DATABASE_URL="postgresql://mesio_app:rehearsal@localhost:54329/postgres" \
  pytest tests/ --ignore=tests/ai_sim -v 2>&1 | head -100
```

### "SAFETY: Current DATABASE_URL does not point to localhost"
Unset or override your env before running:
```bash
unset DATABASE_URL
./scripts/staging_rehearsal.sh --dump /tmp/prod.dump
```

---

## Manual inspection (with --keep)

After a `--keep` run, connect to the rehearsal DB:

```bash
# psql inside container
docker exec -it mesio-rehearsal-pg psql -U postgres

# From host (if psql is installed)
psql "postgresql://postgres:rehearsal@localhost:54329/postgres"

# As mesio_app (respects RLS)
psql "postgresql://mesio_app:rehearsal@localhost:54329/postgres"
```

Useful inspection queries:

```sql
-- Check migration chain
SELECT version_num FROM alembic_version;

-- Org/location counts
SELECT 'organizations' AS t, COUNT(*) FROM organizations
UNION ALL
SELECT 'locations', COUNT(*) FROM locations
UNION ALL
SELECT 'mapping', COUNT(*) FROM _migration_restaurant_to_location;

-- Verify RLS is active
SELECT tablename, rowsecurity, forcerowsecurity
FROM pg_tables WHERE schemaname = 'public' AND rowsecurity = true
ORDER BY tablename;

-- Verify both policies exist on orders
SELECT policyname, cmd FROM pg_policies WHERE tablename = 'orders';

-- Test fail-closed (as mesio_app, no scope)
\c - mesio_app
SELECT COUNT(*) FROM orders;  -- expect 0

-- Test scoped (replace 1 with a real org id)
BEGIN;
SELECT set_config('app.org_id', '1', true);
SELECT COUNT(*) FROM orders;
ROLLBACK;
```

Tear down when done:
```bash
docker rm -f mesio-rehearsal-pg
```
