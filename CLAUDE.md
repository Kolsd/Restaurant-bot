# Mesio Restaurant Bot — v13.0 (head `0102_paid_until`; 1695 passed / 3 skipped with DB (1 skip is date-based: test_subscription_guard needs day ≥ 3), run as `postgres` — the 11 LLM e2e tests fail until there is Anthropic credit)

Multi-tenant SaaS for restaurants (FastAPI + Postgres RLS + Redis + Claude tool_use). Product: the diner's own web channel (QR → `/chat/{table_id}`); WhatsApp was removed on 2026-09-25.

## Token usage — working rules (PM 2026-09-12)
- This file loads on EVERY turn: keep it short. Detail lives in `docs/claude/*.md`, read ONLY when the task touches that topic.
- Updating docs = edit the specific section with Edit. Never re-read or rewrite a whole file for a small change.
- Read files by range/function (Grep → Read with offset/limit). Don't read whole folders or large files.
- Tests: run only the affected files with `-q`; the full suite only before committing bot/repo changes. Never dump full output (`-q --tb=short`, `| Select-Object -Last 30`).
- No agents/subagents unless the PM asks. No long summaries: report in a few lines.
- Suggest `/clear` when switching tasks.

## Read before touching (index of `docs/claude/`)
| If you're touching… | Read |
|---|---|
| State, closed product decisions, next session | `status.md` |
| Env vars, commands | `env.md` |
| `agent*.py`, `orders.py`, `orders_repo.py`, `state_store.py`, `routes/diner.py` | `bot-rules.md` (MANDATORY) |
| Repos, deps, tenant scope, alembic, org/location, branches | `rls-multitenant.md` |
| Structure, DB, webhook/inbox, Redis, scheduler, Decimal, feature flags | `architecture.md` |
| Delivery / pickup web wave, `/pedir`, `/pedido` | `delivery-web.md` (MANDATORY) |
| Wompi / Bold / payments | `payments.md` |
| New tests, DB fixtures, frontend lint | `testing.md` |
| `app/static/**`, visual catalog | `frontend.md` |
| Staff, payroll, shifts, POS | `staff.md` |
| `/internal/*`, HQ, CRM, superadmin | `internal-hq.md` |
| Full style rules | `rules-full.md` |
| Sprint history | `docs/history/` |

## Next session (order)
1. ~~Validate bot with real LLM~~ (2026-09-12: e2e 55/59, sim 12/20 → 15/20 after fixes, commit fefa800). Pending when there is Anthropic credit: one sim run + `test_reservation_lifecycle.py`; the sim doesn't reset the test org's conversation cap. Key in local `.env`.
1b. ~~English codebase~~ (2026-09-13/14: files+URLs 78a2aee, identifiers+comments 5f81d3a, docs). User-facing text stays Spanish; DB columns/values and DOM ids/classes still Spanish (later wave).
1c. ~~Unified staff app `/staff`~~ (2026-09-17, d18118b: admin-dashboard shell, sections per role; old role pages removed; demo seed `scripts/dev/seed_staff_app_demo.py`). Static caching fixed 2026-09-24 (da8c00d: JS/CSS `no-cache`; `sw.js` is DORMANT, CACHE_VERSION has no runtime effect). Orders-rescued and dashboard sales count restaurant-local days since 2026-09-29.
2. ~~Real-time SSE + Redis pub/sub~~ (2026-09-17, bce415a: `app/services/realtime.py`, `/api/staff/stream`, `/api/diner/stream`, `mesio-realtime.js`; polling stays as a 60s net). Prod needs `REDIS_URL` (4 workers).
2b. ~~POS quick-invoice P1~~ (2026-09-17: no org_id + NULL table_id → 500; branch_id fell back to the org id; the check was never claimed so it stayed unpaid).
3. ~~Web delivery wave Phase A~~ (2026-09-19, chunks 1-9: data model, `/pedir/{slug}`, checkout, `/pedido/{code}`, cashier + courier UI, per-sede config, WhatsApp delivery/pickup switched off — spec `docs/claude/delivery-web.md`). Phase B (Mapbox map, live chat) later.
4. ~~Onboarding blockers~~ (2026-09-20): orgs are born with a slug (`/pedir/{slug}` was unreachable for every customer created since 0034; 0088 backfills); the staff roster is no longer behind the `staff_tips` module; `gerente` configures their own sede's delivery; `POST /api/staff/delivery/orders/{id}/mark-paid` records cash/card/transfer (before it, NO web order could ever be `paid=TRUE`, so delivery sales were missing from `total_sales`); CRM convert starts the `comp_until` trial.
4b. ~~Self-serve GTM~~ (2026-09-23/24, GTM decisions in `status.md` #14): real LLM cost per kind (0094), self-serve signup with owner-chosen password + 14-day trial (0095 fixed the CRM `prospects` stub), carta import from photo/text as a DRAFT into the existing editor (editor was opening empty — fixed), owner checklist `/api/onboarding`, QR → `/chat/{id}` + all-tables print sheet. Open: `RESEND_API_KEY` in prod (welcome email is now optional, not blocking); the import needs Anthropic credit to be tried for real; flat price per sede + `plan_code`/`subscription_plan` unification; country-neutral schema.
4c. **Cleanup (2026-09-25, branch `chore/limpieza`, tag `pre-limpieza`)** — scope in memory `cleanup-scope-2026-09-25`. Step 1 done: self-serve orgs (no WhatsApp) could not open a table or `/pedir` → 0096 web key. Step 2 done: WhatsApp deleted (see item 6). Step 3 done (2026-09-26): nothing can set a WhatsApp number/Meta token any more, core diner+delivery tests seed production shape (no number); the `bot_number` → id rename is deferred as no-value tech debt, and clearing prod's stored numbers/tokens (irreversible, needs its own rev after 0097) awaits PM approval. Step 4 done (2026-09-28, walk-through on a DB built from zero): the dine-in loop (signup → carta → mesas/QR → `/chat` → cocina → cuenta → caja → NPS → dashboard) and pickup via `/pedir` now work; fixed kitchen seeing NO table orders (0097: `table_orders.location_id` was never written), waiter bill ×N per alert, dashboard INGRESOS ignoring table sales, carta list not refreshing after save, table names "2-1", fake "Sugerencia IA" card, no staff-app link for owners. Findings left for later in memory `walkthrough-2026-09-28`. Step 5 done (2026-09-29, 4 commits): deleted loyalty/marketing/at-risk/AI insight/discounts, menu engineering/public reviews/weekly report/unused stats/AI proxy, payroll/shifts/clock-in/WebAuthn/kiosk/"Mi turno"/staff comms, HQ analytics page (HQ home keeps overview+MRR+rescued). KEPT by PM: DIAN e-invoicing and offline sync. DB tables stay until a later drop migration. Step 5b done (2026-09-29, 3 commits): the bot keys on `org_id` everywhere (0098), 0099 drops `bot_number`, the WhatsApp numbers and Meta tokens and gives `reservations` RLS; reservations now store their sede; dead WA-era code/routes deleted; e2e harness + AI sim seed by slug; fixed deps caching the user on the shared `request.state` (a second owner got the first owner's org in e2e — `test_cross_tenant_rls_leak` passes now). Mesio's own support/legal contact stays on `wa.me` (PM decision); `scripts/load_test.py` has been broken since Wave 2. Conversation-cap P1 done (2026-09-29, 399527e): the cap only counts + alerts Mesio, never redirects diners or charges packs; auto-recharge/buy-pack removed (terms.html + landing still mention packs — rewrite in step 6). Step 6a done (2026-09-30): new landing + terms + signup on the pricing in `status.md` #15, trial 14→15 days. Plan wave 1 done (2026-09-30): price list in `services/plans.py` + 0101, plan_code unified, founder price, MRR per sede. Plan wave 2 done: per-plan gating via `services/plan_access.py` (Esencial: no AI chat — free text gets the carta, no LLM call —, no /pedir, 5 staff/sede; reservations/inventory/DIAN from Pro; trial = at least Restaurante; orgs created without a plan are Restaurante). Plan wave 3 done: subscription state `plans.billing_status` (trial → activo → vencido 7 days → suspendido; no dates = managed by hand, activo) from `comp_until` + `paid_until` (0102); superadmin records payments (+1/+12 months); a paused org takes no diner sessions/chat/orders; HQ notifications for ending trials, overdue and paused. Step 6b done (2026-10-01): live demo `/demo` (`services/live_demo.py`): "Casa Mesio" self-provisions on the first visit (slug `casa-mesio-demo`, Esencial = no LLM cost, 20 tables, oldest session recycled when full); the visitor scans the QR, orders by tapping, watches the ticket on the demo kitchen and marks it ready; the diner chat now announces listo/entregado (it never did). Next: deploy (0101/0102) + first restaurant conversations. The e2e flagship (`test_happy_path_full_flow`) passes again: its "pre-existing" failure was a MagicMock usage missing the 0094 cache-token fields.
5. ~~jsonb double-encoding P1~~ (2026-09-29): the pool codec now passes a `str` through as JSON text (`database._encode_jsonb`), so `json.dumps(x)` and `x` both store once; 0100 unwrapped every stored JSON-string object/array in all jsonb columns; test pools use `init_connection`; `tests/test_jsonb_codec.py` guards it.
6. ~~Turn off remaining WhatsApp~~ (2026-09-25, cleanup step 2: webhook, inbox worker, Meta API, audio, WA catalog/QR claims, CRM WA inbox, scheduler WA tasks; e2e harness + AI sim now drive `agent.chat()` like the web chat). Identity cleanup: step 3 above.
7. Fix the Wompi webhook before reactivating it (its e2e test fails: invalid signature → 200, expects 401).
Closed product decisions: see `status.md` — do not re-discuss.

## Commands
```bash
uvicorn app.main:app --reload --port 8000   # local: use your own launcher, do NOT overwrite .claude/launch.json
alembic upgrade head                        # always a single head (`alembic heads`)
pytest tests/<file>.py -q                   # with TEST_DATABASE_URL/DATABASE_URL/DATABASE_URL_ADMIN pointed at the test DB
python scripts/lint_frontend.py             # before committing static
```
Local Windows environment: `.venv` Py 3.12, Postgres 16 (`postgres`/`mesio_local_dev`; `mesio_app`/`mesio_app_pw`), DBs `mesio_tests`/`mesio_test`/`mesio_fresh` in UTC, no Redis. Pins: `pytest==8.4.2`, `pytest-asyncio==0.24.0`.

## Non-negotiable rules (summary; detail in the docs)
- **SQL** only in `app/repositories/`, `$n` parameters, never f-string with values.
- **RLS**: `async with tenant_connection()` + `tenant_scope(org_id)`; cross-tenant only via `bypass_tenant_scope("reason≥8")`. Never `get_pool()` directly in new repos, never catch `TenantNotSetError`. New tenant table → RLS ENABLE+FORCE in the migration.
- **Org vs location**: `org_id` and `location_id` are distinct integers; never guess or fall back between them. `db_get_restaurant_by_location_id` / `_by_org_id` per intent. Tenant tests seed ids that collide on purpose.
- **Sede scoping**: every staff-facing listing filters by sede via `deps.resolve_sede_filter`. Only `owner`/`admin` may span sedes or pick one with `X-Branch-ID`; everyone else — `gerente` included — is pinned to their own `location_id`, and no sede means 403, never org-wide. Never add a new header read. Detail: `rls-multitenant.md`.
- **Inventory is per sede**: stock rows carry `location_id`, creating one requires naming a sede, and "the same product at another sede" is matched by `lower(name)` within the org — used by both transfers and order deduction. Pass `location_id` to `deduct_inventory_in_tx`. Detail: `rls-multitenant.md`.
- **Sede name**: `restaurants.display_name` ("Marca · Sede" only when the org has several) is what a restaurant calls itself to a human; `name` stays the org's. Python mirror: `services/naming.py`.
- **"Agotado" is per sede**: `menu_availability` is keyed `(org_id, location_id, dish_name)`. `db_get_menu_availability` REQUIRES a `location_id` and `db_set_dish_availability` raises without one — no org-wide fallback. Only the public `/r/{slug}` brand pages use `db_get_menu_availability_any_sede`.
- **The carta is per sede**: the org's `organizations.menu` plus each sede's overrides (price, hidden, own dishes; 0093). Anything that shows or prices a dish at a sede reads `services/sede_menu.get_sede_menu(org_id, location_id)`, never the base `db_get_menu(org_id)`; the bot gets its sede from `sede_context`. Gerente edits their own sede; the base belongs to owner/admin. Detail: `rls-multitenant.md`.
- **Money**: `Decimal` + `services/money.py`; `float` only at the JSON edge (`# JSON boundary`). Never Decimal in `state_store`.
- **4 workers**: mutable state via `state_store` (Redis); never hold a DB connection or transaction across an LLM call.
- **Logging**: `get_logger(__name__)`, typed catch, no `except Exception: pass`, no `print`, `mask_phone()` in logs.
- **Frontend**: `textContent` for user data; `mesioHeaders()`/`_staffFetch`; a commit touching static → bump `CACHE_VERSION` in `sw.js` in the same commit.
- **Alembic**: rev id ≤ 32 chars; `sa.text()` + `CAST(:p AS type)`; `IF NOT EXISTS`.
- **Truthful tests**: no silently-skipped tests, no `status_code == 200` as the only assertion, no mocking the whole repo. Dates in UTC. Gateways: signatures checked against the real doc/event.
- **If you see it, you fix it** (or ask if it's a product decision / expands scope by >50%).
- **Bot LLM = always Haiku 4.5** (PM decision 2026-09-12). Bot failures get fixed in code/guards/prompts, never by upgrading the model.
