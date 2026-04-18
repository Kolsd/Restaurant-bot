# Migration 0037 — Railway Rehearsal Guide

Validates that migration `0037_drop_legacy_rls_and_restaurant_id` is safe to apply
to production by running the full pipeline against the Railway **Test** Postgres service.
Prod data is read-only (via `pg_dump`). The test DB is fully disposable.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Railway `Postgres` service | Prod DB — must have `DATABASE_URL_ADMIN` env var set to the superuser URL |
| Railway `Test` service | Empty Postgres DB — you need its connection string |
| `psql` / `pg_dump` | Already installed in the Railway nixpacks Python image |

## Environment Variables to Set on the Web Service

Before triggering rehearsal, add these two env vars on the **Restaurant-bot web service**:

```
TEST_DATABASE_URL=postgresql://postgres:<password>@<test-host>:<port>/railway
REHEARSAL_MODE=1
```

`DATABASE_URL_ADMIN` should already be set (used for prod read).

## Triggering

1. Set `REHEARSAL_MODE=1` and `TEST_DATABASE_URL=<test-db-url>` on the Railway web service.
2. Click **Deploy** (or push a no-op commit). Railway will run the rehearsal script instead of uvicorn.
3. Open the **Deploy Logs** tab and watch the output in real time.
4. The deploy will exit when the script finishes (exit 0 = pass, exit 1 = fail).

## Reading the Output

Each stage prints a header:
```
================================================================
STAGE 2 — Copy prod → test (pg_dump | psql)
================================================================
```

Each smoke check prints `✓` (pass) or `✗` (fail):
```
  ✓ [6a] alembic head = 0037_drop_legacy_rls
  ✗ [6f] tenant_isolation policy still exists on 2 table(s)
```

The final summary box shows overall result:
```
╔══════════════════════════════════════════════════════╗
║  REHEARSAL RESULT: PASSED (15/15 checks)             ║
║  Duration: 187s                                      ║
║  Next: unset REHEARSAL_MODE, redeploy, then          ║
║  apply 0037 to prod via Wave 2F.                     ║
╚══════════════════════════════════════════════════════╝
```

## On PASS

1. Unset `REHEARSAL_MODE` on the Railway web service.
2. Unset `TEST_DATABASE_URL` (optional, harmless to keep).
3. Redeploy normally — Railway will run the standard `alembic upgrade head && uvicorn ...` path.
4. Proceed with Wave 2F: rename `0037_drop_legacy_rls_and_restaurant_id.py.deferred` → `.py`
   and deploy to production.

## On FAIL

1. Note which check IDs failed (e.g. `6c`, `6i`).
2. Share the full Railway deploy log.
3. Do NOT undefer 0037 in production.
4. The test DB can be left dirty — next rehearsal run will `DROP SCHEMA public CASCADE` first.

## How It Works (summary)

```
Stage 1  Preflight: assert env vars, safety check URLs differ
Stage 2  pg_dump prod | psql test  (10 min timeout)
Stage 3  GRANT DML to mesio_app on test DB
Stage 4  Rename 0037 .deferred → .py  (reverted in finally block)
Stage 5  alembic upgrade head against test DB
Stage 6  Smoke checks 6a-6o (asyncpg)
Stage 7  app.main import smoke (non-fatal)
Stage 9  Rename 0037 .py → .deferred  (always runs, even on crash)
```

The `0037` migration file is renamed back to `.deferred` in a `finally` block,
so it can never accidentally be committed as `.py` after rehearsal.

## Reverting After a Bad Run

If the script crashes mid-stage and `0037_drop_legacy_rls_and_restaurant_id.py` is left active
(check `alembic/versions/`), rename it back manually:

```bash
mv alembic/versions/0037_drop_legacy_rls_and_restaurant_id.py \
   alembic/versions/0037_drop_legacy_rls_and_restaurant_id.py.deferred
```

Then commit and push so Railway picks up the correct state.
