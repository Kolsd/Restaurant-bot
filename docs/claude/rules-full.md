# Full style rules and critical instructions

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Security and Style Rules

- **SQL**: f-strings to inject values are FORBIDDEN. Always positional `$1, $2, ...`. Accepted exception: f-strings only to build dynamic `SET col=$n` clauses in updates (see `db_update_deduction_item`), never for user values.
- **Auth**: JWT 72h. Bcrypt passwords. Users (email/pass) vs Staff (name+PIN). Staff token = `staff:<uuid>`. Admin sessions stored as a SHA-256 hash.
- **XSS**: In JS use `textContent` for user data, never `innerHTML`. `innerHTML` only for static strings with no external data.
- **JSONB**: asyncpg auto-encodes. Don't use `json.dumps()` except where the driver explicitly requires it (e.g. passing a dict as `$n::jsonb`).
- **NULL in SQL**: `IS NULL` / `IS NOT NULL`. Never `WHERE col = NULL`.
- **Fetch in JS**: Always use `_staffFetch(path, method, body)` or `mesioHeaders()` instead of raw `fetch()`.
- **AI API**: Calling Anthropic/OpenAI from the browser is FORBIDDEN. Use the `POST /api/ai/proxy` proxy (auth required, server-side).
- **Logging**: `except Exception: pass` is FORBIDDEN. Use `from app.services.logging import get_logger; log = get_logger(__name__)`. Typed catch + `log.exception("context.event", **ctx)`. If it affects data/money consistency → re-raise after logging.
- **Money**: `float` in financial arithmetic is FORBIDDEN. Use `Decimal` + the helpers in `services/money.py`. `float(...)` only at the JSON edge with the comment `# JSON boundary`.
- **Prompt injection**: Any new place where user text gets injected into the LLM must go through `_wrap_user_message(...)`.
- **Multi-tenant RLS (Phase 1)**: `pool.acquire()` / `get_pool()` directly in new repos is FORBIDDEN — use `async with tenant_connection() as conn:`. The call site must enter `tenant_scope(rid)` (admin/staff routes via `_scoped` deps) or `bypass_tenant_scope("reason")` (internal/scheduler/inbox pre-resolve). Catching `TenantNotSetError` is FORBIDDEN (it's the signal for a call site missing scope). A new table with `restaurant_id NOT NULL` MUST be added to `_RLS_TABLES` in a migration that enables + forces RLS.
- **DB URLs**: the runtime app connects as `mesio_app` (non-superuser) via `DATABASE_URL`. Alembic runs with `DATABASE_URL_ADMIN` (postgres superuser). Pointing the runtime app at a superuser URL is FORBIDDEN — it invalidates RLS enforcement.

## Critical Instructions for Claude Code
- **No vagueness**: When in technical doubt, ask before proposing massive token-consuming changes.
- **Multi-worker isolation**: When modifying state (`NPS`, `checkout`), always assume there are 4 workers and use `state_store` (Redis).
- **Repository pattern**: SQL is forbidden in `app/routes/` and `app/services/` (except `billing.py` for fiscal). All new SQL goes in `app/repositories/`.
- **Multi-tenant RLS (v11.0)**: Any new repo touching a table with `restaurant_id` MUST use `async with tenant_connection() as conn:` and be called from a call site with an active `tenant_scope(rid)` (or `bypass_tenant_scope("reason")` if it's genuinely cross-tenant). READ the "Multi-tenant RLS hardening" section before touching repos, deps, bot runtime, or alembic. This state has been empirically verified — don't break it with "silent fails" or generic catches.
- **Financial precision**: Using `float` for money is forbidden. Use `Decimal` and the helpers in `app/services/money.py`.
- **Strict logging**: Use `structlog` via `get_logger(__name__)`. `print()` and bare `except Exception: pass` blocks are forbidden.
- **Migrations**: Always use `IF NOT EXISTS` so Railway's start command never fails. Alembic runs with `DATABASE_URL_ADMIN` (superuser); the runtime app connects with `DATABASE_URL` (mesio_app non-superuser).
- **Alembic patterns (3 footguns that broke prod on 2026-05-06)**:
  1. **Multiple heads**: if two parallel sprints create migrations with the same `down_revision`, `alembic upgrade head` fails with "Multiple head revisions are present". BEFORE merging to main, run `alembic heads` — it must return 1 line. If it returns 2+, create a no-op merge migration (see `0072_merge_plan_limits_demo_seed.py` as the pattern) with `down_revision = ("rev_a", "rev_b")` and empty `upgrade`/`downgrade`.
  2. **`conn.execute("string")` doesn't work in SQLAlchemy 2.0**. Use `import sqlalchemy as sa` + `conn.execute(sa.text("..."))`. `op.execute("string")` DOES accept raw strings (alembic converts them) but `op.get_bind().execute(...)` doesn't — they're different APIs. Canonical pattern in the repo: `0034_create_organizations_locations.py`.
  3. **`:param::type` breaks with `sa.text()` bound params**. SQLAlchemy's regex gets confused by the `::` (cast operator) and leaves `:param` literal in the final query → `psycopg2.errors.SyntaxError`. Use `CAST(:param AS type)` instead. E.g.: `INSERT ... VALUES (CAST(:menu AS jsonb))` ✅, NOT `:menu::jsonb` ❌.
- **Bot is Untouchable**: READ the "Bot Rules — DO NOT BREAK" section BEFORE touching any bot file. Every rule exists because of a real bug that hit customers.
- **Mandatory Tests**: After any change to bot files, run `pytest tests/ --ignore=tests/ai_sim`.
  - **With `TEST_DATABASE_URL` exported** (DB in UTC, migrated to head): **1741 passed / 0 failed / 6 skipped** in ~2.5 min (no e2e).
  - **Without `TEST_DATABASE_URL`**: **1400 passed / 0 failed** (~350 skipped: the DB tests). Never take this number as proof of health: it doesn't exercise the DB.
  - **E2E tests (`tests/e2e/`)**: require `TEST_DATABASE_URL` + `ANTHROPIC_API_KEY` (the `e2e_no_llm` ones only need the DB). Run them serially — they're slow due to real seeding + real Anthropic calls.
  - Any new failure is a real regression — don't merge until it's resolved.
- **Claim-then-ack**: NEVER revert inbox_worker to a long transaction. The 3-phase pattern exists to prevent pool deadlock.
- **Frontend Lint ("No-v2" sprint)**: Before merging any change touching `app/static/js/**` or `app/static/html/**`, run `python scripts/lint_frontend.py` — CI fails if it finds mock/TODO/dead-fetch/seed-data. Legitimate suppression via `// lint-allow: reason` (JS) or `<!-- lint-allow: reason -->` (HTML). Suppressing without an explicit reason is FORBIDDEN.
- **Truthful Tests**: New integration tests against `TEST_DATABASE_URL` MUST test aggregate correctness (seed data → assert the exact value), tenant isolation (org A vs org B), and the empty case (no 500). Forbidden: `assert status_code == 200` as the only assertion, mocking the whole repo, `assert "key" in data` without checking the value. See the reference fixture in `tests/test_loyalty_aggregates.py` ("No-v2" sprint).
- **Native Tool Use**: The bot uses Claude's tool_use API. NEVER go back to JSON-in-prompt. `_validate_tool_call()` is the safety barrier.
- **Checkout State Machine**: Before modifying `handle_checkout_flow`, mentally draw out every step and verify each one has a branch. A step without a branch = broken checkout.
- **"If you see it, you fix it" rule (PM 2026-04-29)**: If during a session you find something broken, odd or suspicious — even if pre-existing, even if it's not literally in the task's scope — you do NOT write in the report "it's pre-existing", "not mine", "out of scope", "historical debt". You fix it, or if it requires a product decision (not a technical one), you ask the PM first. Every problem seen and not fixed is debt that resurfaces. Legitimate exception: if fixing it would expand the scope by >50% of the original work, note it as a concrete sub-task with file/lines (not a handwave) and ask the PM whether to proceed.
