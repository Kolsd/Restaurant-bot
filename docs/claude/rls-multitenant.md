# Multi-tenant: Postgres roles, RLS, Wave 2 org/location, branches

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Postgres roles (RLS Phase 1)

- **`postgres`** (superuser) — used ONLY by Alembic via `DATABASE_URL_ADMIN`. Implicit RLS bypass.
- **`mesio_app`** (non-superuser, LOGIN) — the app's runtime connection. RLS applies normally. Has DML + sequences + execute + `mesio_superadmin` granted.
- **`mesio_superadmin`** (BYPASSRLS, NOINHERIT, no LOGIN) — activated via `SET LOCAL ROLE mesio_superadmin` inside `bypass_tenant_scope()`. Used for internal routes, the scheduler leader tick, inbox worker pre-resolution, and cross-tenant analytics.

## Multi-tenant RLS hardening — Security Roadmap Phase 1 (v11.0)

**Goal:** make it impossible to leak cross-tenant data even if a dev forgets `WHERE restaurant_id = $1`. The real enforcement lives in Postgres RLS; Python is just the plumbing that feeds the GUC.

### Current state: 100% applied

| Layer | Mechanism | File/migration |
|---|---|---|
| App fail-fast | `TenantNotSetError` if you call `tenant_connection()` without a scope | `app/services/tenant_context.py` |
| App→DB | `SET LOCAL app.restaurant_id = $1` on every `tenant_connection` | `app/services/tenant_db.py` |
| DB read | `USING (restaurant_id = NULLIF(current_setting('app.restaurant_id', true), '')::int)` | Alembic 0029 |
| DB write | `WITH CHECK (...)` — blocks spoofing restaurant_id on INSERT/UPDATE | Alembic 0029 |
| Owner lockdown | `ALTER TABLE ... FORCE ROW LEVEL SECURITY` | Alembic 0030 |
| Non-superuser runtime | The app connects as `mesio_app` (DML-only), RLS applies | `.env` + `alembic/env.py` |
| Admin escape hatch | `bypass_tenant_scope("reason")` → `SET LOCAL ROLE mesio_superadmin` (BYPASSRLS) | `app/services/tenant_context.py` |

**Empirical proof (on the current DB, tested in session 2026-04-15):**
```
mesio_app + scope=8  → orders=4   (only that tenant)
mesio_app + scope=19 → orders=11
mesio_app + no_scope → orders=0   (fail-closed)
INSERT without scope     → InsufficientPrivilegeError (WITH CHECK fires)
INSERT cross-tenant  → InsufficientPrivilegeError (scope=8 trying restaurant_id=19)
bypass_tenant_scope  → orders=15 (everything)
```

### Wave-2 model: there is NO Matriz (head-office) entity. No "primary" either.

Post-Wave-2 the canonical schema is `organizations` + `locations`. **Every location is a peer of the others** — there's no "head office", no "main one". An org has N locations, all operationally equivalent.

`locations.is_primary` exists in the schema **only as migration scaffolding** (to map "which old location was the head office" during the 0034 backfill). **It's vestigial. Do NOT use it in new code.**

#### Rules for new code

1. **Enumerating businesses** → `db_get_all_orgs()` (returns org rows). NOT `db_get_all_restaurants()`, which filters by `is_primary=true` and perpetuates the old model.
2. **Enumerating a business's locations** → `db_get_org_locations(org_id)` (all locations, not just "primary").
3. **NEVER filter by `is_primary = true`** to find "the main restaurant". That question doesn't make sense in the new model. If you need a deterministic default (e.g. "the org's first location"), use `ORDER BY id ASC LIMIT 1` — it's not "the primary", it's just "a deterministic one".
4. **NEVER rely on the "Matriz invariant"** (`org_id == matriz_location_id`). It only holds for orgs migrated by 0034 (existing at Wave-2 deploy time). For orgs created AFTER the deploy, `org_id` and `location_id` are independent integers (auto-incremented separately).
5. **NEVER assume `restaurant.get("location_id") or org_id` is a valid fallback.** It's the "Matriz invariant trick" in disguise — it gives the wrong answer for new orgs.
6. **Correct resolution** of location_id when you need the location:
   - If the dict came from `db_get_restaurant_by_id` or `db_get_all_restaurants` → use `restaurant["location_id"]` (always populated post-Step 7).
   - If you don't have a dict → query `SELECT id FROM locations WHERE org_id = $1 ORDER BY id ASC LIMIT 1` (any location, no "primary" value judgment).
7. **`parent_restaurant_id IS NULL` is legacy emulation.** The `restaurants` VIEW exposes it for backwards compat with old code. For new queries, use `db_get_all_orgs()` directly.
8. **The `is_main_restaurant` parameter is vestigial.** Do not introduce it in new code.
9. **`X-Branch-ID` header ALWAYS carries a `location_id`**. If a route receives it, do NOT mix it with `restaurant["id"]` (= org_id) — they're two distinct integers. See Step 5 commits da08f5e + Step 6 commit c442bba.

#### Deprecation status (live)

| Symbol | Status | Replacement |
|---|---|---|
| `db_get_all_restaurants()` | DEPRECATED for the "list businesses" semantic | `db_get_all_orgs()` |
| `locations.is_primary` (column) | VESTIGIAL — only for migration backfill | (nothing — locations are peers) |
| `parent_restaurant_id IS NULL` filter | LEGACY EMULATION (via VIEW) | `db_get_all_orgs()` |
| `is_main_restaurant` parameter | VESTIGIAL | (drop) |
| "Matriz invariant" fallback | REMOVED (Step 7) | Explicit `restaurant["location_id"]` |
| `db_get_restaurant_by_id()` | **DELETED 2026-09-11** — accepted EITHER location_id OR org_id and on collision returned a DIFFERENT restaurant (cross-tenant P0) | `db_get_restaurant_by_location_id()` / `db_get_restaurant_by_org_id()` — pick by intent, never guess. Guard: `tests/test_no_ambiguous_restaurant_lookup.py` |
| `users.branch_id` | AMBIGUOUS (no FK; some writers stored org_id, others location_id) | `users.org_id` + `users.location_id` (migration 0081, with FK). Auth DENIES access if `org_id` couldn't be resolved — never a fallback |
| `db_update_restaurant_fields()` / `db_update_subscription()` and `POST /api/internal/admin/update-restaurant` + `/set-subscription` | **DELETED 2026-09-12** — resolved the organization via a location subquery; on id collision they wrote to a DIFFERENT customer | `PATCH /api/internal/admin/organizations/{org_id}`. Guard: `tests/test_no_legacy_restaurant_field_writes.py` |

### Usage pattern

**In FastAPI routes (authenticated admin/staff):**
```python
# Restaurant admin (owner/manager) — scope from the restaurant dict
@router.get("/api/loyalty/balance")
async def get_balance(
    phone: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),  # ← yield-based, enters tenant_scope
):
    return await db.db_get_loyalty_balance(restaurant["id"], phone)

# Staff authenticated with a "staff:<uuid>" JWT — scope from user["restaurant_id"]
@router.get("/api/staff/self/timecard")
async def my_timecard(user: dict = Depends(get_current_user_scoped)):
    return await db.db_get_staff_timecard_rows(user["restaurant_id"])
```

**In the bot runtime (Meta webhook):**
```python
# inbox_worker._handle_meta_whatsapp — after resolving the restaurant from bot_number
if _tenant_id is not None:
    with tenant_scope(_tenant_id):
        await _process_message(...)
```

**In cross-tenant routes/services (internal, scheduler, analytics):**
```python
# app/routes/internal/admin.py — Mesio superadmin
with bypass_tenant_scope("internal_admin_restaurants_list"):
    return await db.db_get_all_restaurants()

# scheduler leader tick — enumerates restaurants, then scopes per one
with bypass_tenant_scope("scheduler_leader_tick"):
    restaurants = await db_get_all_restaurants()
    for r in restaurants:
        with tenant_scope(r["id"]):
            await _per_restaurant_task(r)

# chat.py Meta webhook — enqueue happens pre-tenant
with bypass_tenant_scope("webhook_enqueue_cross_tenant"):
    await inbox_repo.enqueue(...)
```

### Repo classification

| Repo | Type | Notes |
|---|---|---|
| `loyalty_repo`, `fiscal_repo`, `discounts_repo`, `customer_profiles_repo` | 100% tenant-scoped | `_get_pool` removed |
| `orders_repo`, `conversations_repo`, `inventory_repo`, `reviews_repo`, `reservations_repo`, `reservation_deposits_repo`, `weekly_reports_repo`, `menu_analytics_repo` | 100% tenant-scoped | `_get_pool` removed |
| `staff_repo`, `tables_repo` | Tenant-scoped with internal `bypass_tenant_scope` in ~20 functions | 🚧 debt: audit each internal bypass (public kiosk vs. questionable) |
| `marketing_repo` | MIXED: `marketing_messages_log` tenant; `prospects`/CRM GLOBAL | Keeps `_get_pool` for GLOBAL |
| `restaurant_repo` | MIXED: 14 tenant (per-restaurant config), 38 GLOBAL (users, enumeration, bot pre-resolution) | Keeps `_get_pool` for GLOBAL |
| `sessions_repo` | GLOBAL | `sessions` has no `restaurant_id` (cross-tenant auth). DO NOT MIGRATE. |
| `inbox_repo` | GLOBAL | `webhook_inbox` is pre-resolution by design. DO NOT MIGRATE. |
| `crm_repo` (`app/repositories/internal/`) | GLOBAL | Internal Mesio tooling. DO NOT MIGRATE. |

### Rules any future change MUST respect

1. **Never use `get_pool()` / `pool.acquire()` directly in new repo code.** Use `tenant_connection()` + `tenant_scope(rid)` at the call site.
2. **Never catch `TenantNotSetError`.** It's the design signal — if it fires, there's a call site missing scope.
3. **Every new table with `restaurant_id NOT NULL` MUST be added to `_RLS_TABLES` in a new migration** that enables RLS + FORCE. If you forget, the table stays unprotected.
4. **`set_config` parameters are always positional, never f-string.** `SET LOCAL` via `SELECT set_config('app.restaurant_id', $1, true)`.
5. **Alembic migrations run with `DATABASE_URL_ADMIN` (superuser).** The runtime app must NEVER point at a superuser URL.
6. **New tests mock `app.services.database.get_pool`** and wrap in `tenant_scope(N)`. The old pattern (`monkeypatch.setattr(repo, "_get_pool", ...)`) breaks because `_get_pool` was removed from migrated repos. Reference: `tests/test_loyalty_repo_tenant.py`.
7. **`bypass_tenant_scope` ALWAYS with a reason ≥ 8 chars.** It's logged for audit. Reserved for: `/api/internal/*` routes, the scheduler leader tick, inbox worker pre-resolution, agent.py cross-tenant pre-scope lookups, public kiosk endpoints (WebAuthn).

### Pending debt (non-blocking)

- Audit the ~20 internal `bypass_tenant_scope` calls in `staff_repo.py` — several are questionable (breaks, self-profile) and could be tightened to `tenant_connection()` if the call site always enters with a scope.
- ~~17 integration tests + 26 more in other files~~ — **CLOSED 2026-04-19**: refactored with the `_ConnProxy` pattern (workaround for `asyncpg.Connection.__slots__`) + INSERTs via `organizations + locations` (no more writing to the `restaurants` VIEW) + `org_id` columns (no more `restaurant_id`). 46 tests passing post-refactor against TEST_DATABASE_URL.
- ~~6 pending X-Branch-ID conflation sites in `staff.py`~~ — **CLOSED in Step 10**. All 6 sites were migrated to the fix template: consistent org_id for org-level queries, location_id propagated to `db_calculate_payroll`'s optional `branch_id` param for per-location tip scoping.
- Phase 2 (integrity/concurrency) and Phase 3 (AI decoupling + middleware) — ✅ shipped 2026-04-17. The original plan was removed from the repo once the last item closed.

## Sede (location) scoping — MANDATORY for every staff-facing listing

PM decision 2026-09-20: **an employee of one sede must never see another
sede's data.** Two tiers, and exactly one place decides which you are in:

| Role | May see |
|---|---|
| `owner`, `admin` (`deps.SEDE_SPANNING_ROLES`) | every sede of their org, or ONE picked with `X-Branch-ID` / `X-Location-ID` |
| everything else, **including `gerente`** | their own `staff.location_id` / `users.location_id`, whatever the header says |

Use `app/routes/deps.py::resolve_sede_filter(request, user)`. It returns the
`location_id` to filter by, `None` for "every sede" (admins only), or raises
403 when a non-admin has no sede — never fall back to org-wide.
`may_span_locations(user)` is the role test on its own. `allow_all_sentinel=True`
adds the `"all"` / `"matriz"` strings the stats/NPS/loyalty repos expect.

Do NOT write a new header read. Before this existed, a dozen routes each
re-read `X-Branch-ID` with their own rules and most applied no role check at
all, so a waiter could name another sede — or name none and be served the
whole org, which is what the staff app did by default. `X-Branch-ID` is
honoured inside `get_current_restaurant` for admins only, so anything that
derives its sede from the returned restaurant row is already scoped.

`gerente` is an admin role in `staff_sections.ADMIN_ROLES` (which sections
of the staff app they see) but NOT in `SEDE_SPANNING_ROLES` (which sedes
they may read). The two sets are deliberately different — don't merge them.

Covered so far: `/api/table-orders`, `/api/waiter-alerts`,
`/api/tables/floor-plan`, `/api/checkout-proposals`, `/api/staff` (roster),
`/api/team/users`, `/api/dashboard/*`, `/api/stats/*`, `/api/nps/*`,
`/api/loyalty/*`, `/api/reservations/*`, `/api/pos/*` (via
`get_current_restaurant`), plus delivery, which already had it.
Inventory too, since the PM answered the write-side question (see below).
Marketing, discounts and reviews are still untouched — owner-facing surfaces,
not things a sede employee opens.

### Inventory is per sede (PM 2026-09-20)

"El inventario es uno por sede, se puede hacer intercambios de inventario por
sede. Si el owner quiere añadir inventario deberá escoger la sede primero con
un selector de sedes."

- `inventory.location_id` is written on every create. The API refuses a create
  with no sede (400) when the caller can span sedes and has not picked one;
  for anyone else it is their own sede and a `location_id` in the body is
  ignored. Migration 0090 backfills existing rows to the org's first location.
- Reads (`/api/inventory`, `/api/inventory/alerts`, `/api/stats/inventory-critical`)
  filter by sede. Editing, deleting, adjusting and reading history all go
  through `_owned_item_or_404`, which checks the SEDE, not just the org.
- **"The same product at another sede" is matched by `lower(name)` within the
  org.** Rows are per-sede, so sede A's "Tomate" and sede B's "Tomate" are two
  ids; the name is the only thing tying them together. This one rule is used
  in two places — do not invent a second one:
  1. `db_transfer_inventory` (POST `/api/inventory/{id}/transfer`), which moves
     stock in a single transaction and CREATES the row at the destination sede
     when that sede never stocked the product.
  2. the order deduction path. `dish_recipes` stays org-level (a dish is made
     the same way everywhere), so `ingredient_id` points at the row of
     whichever sede the recipe was written in; `deduct_inventory_in_tx` and
     `db_deduct_inventory_for_order` resolve it to the row of the sede that is
     actually cooking, via `location_id`. Pass it — the default None is the old
     org-wide behaviour, kept only for callers with no sede in hand.
- Transfers: the SOURCE must be a sede the caller may act on (so a gerente
  sends out of their own sede but cannot pull from another); the DESTINATION
  can be any sede of the org.
- ~~`menu_availability` is keyed `(dish_name, org_id)`~~ — closed by
  migration 0091, see below. A transfer still deliberately does not touch
  availability: moving stock between sedes does not change what the org has.

### The sede's own name (PM 2026-09-20, migration 0092)

`restaurants.display_name` is "Marca · Sede" for an org with more than one
location and the plain brand for an org with one. Use it wherever a
restaurant names itself to a human — chat greeting, `/pedido/{code}`,
customer emails, dashboard header. `name` is UNCHANGED (still the org's) and
still what tenant resolution and older templates read; do not flip it.

`app/services/naming.py::restaurant_display_name` is the in-Python mirror
for the paths that merge an org row with a location row instead of reading
the view (`diner.py::_resolve_diner_restaurant`). `tests/test_sede_display_name.py`
asserts the two do not drift.

Guards in both: a single sede, a blank sede name, or a sede named after the
org all fall back to the plain brand.

### "Agotado" is per sede (PM 2026-09-20, migration 0091)

`menu_availability`'s primary key is `(org_id, location_id, dish_name)` and
`location_id` is NOT NULL. Before 0091 it was `(org_id, dish_name)`, so
marking a dish sold out at one sede took it off every sede's menu — including
automatically, whenever one sede's inventory hit its minimum.

- `db_get_menu_availability(org_id, location_id)` — `location_id` is
  REQUIRED. There is no org-wide fallback; that fallback is the bug.
- `db_set_dish_availability(..., location_id=...)` raises without a sede.
- `db_get_menu_availability_any_sede(org_id)` is the ONE exception: the public
  `/r/{slug}` brand pages have no sede yet, so a dish stays listed while at
  least one sede has it.
- The sync helpers (`_sync_dish_availability*`, `_sync_ingredient_dishes_conn`,
  `_recheck_dishes_for_ingredient_conn`) all take the sede and log-and-skip
  without one, rather than re-creating an org-wide row.
- `db_upsert_dish_recipe` is org-level but re-evaluates the dish once PER SEDE
  (`_resync_dish_for_every_sede_conn`), each against that sede's own stock.

Every caller must have a sede: diner sessions carry `location_id`, the
WhatsApp/table lookups get it from `db_get_restaurant_by_phone` (which returns
`location_id` alongside the org in `id`), and the owner's sold-out toggle
(`/api/menu/availability`) refuses with 400 until a sede is picked.

### The carta is the org's, with per-sede changes (PM 2026-09-21, migration 0093)

`organizations.menu` stays the ONE base carta. On top of it each sede may:
- charge its own price — `location_menu_overrides.price` (NULL = base price).
  It survives later base-price changes until someone removes it;
- hide a dish — `location_menu_overrides.hidden`;
- sell dishes of its own — `location_menu_dishes` (full dish JSONB + category).
  A name the base already has is refused; if the base later gains one, the
  sede's own dish wins.

Matched by dish NAME, case-insensitive (unique on `lower(dish_name)`), like
`menu_availability`. Both tables: RLS `org_isolation` + FORCE, FK to
`locations` ON DELETE CASCADE.

- **Reading a sede's carta = `services/sede_menu.get_sede_menu(org_id, location_id)`.**
  Never `db_get_menu(bot_number)` for anything that shows or prices a dish at a
  sede: it returns the base and, for a chain sharing one number, can't tell
  sedes apart. `location_id=None` returns the base (brand pages only).
- **The bot** reads the carta deep in its call chain. `agent.chat` opens a
  `sede_context` turn and names the sede once the restaurant resolves
  (`set_sede`); `orders._turn_menu` / `find_dish` read it. A read with no sede
  falls back to the base and logs `menu.read_without_sede`.
  `_load_restaurant_context` now honours the web session's `location_id`.
  Before, a table-less (/pedir) turn used the org's default sede.
- **Who edits:** `PUT /api/menu/update` (base) → owner/admin only.
  `/api/menu/sede*` (`routes/sede_menu_routes.py`) → owner/admin for the sede
  they pick, gerente pinned to their own (via `resolve_sede_filter`). UI: the
  "Carta de esta sede" tab in `/menu-admin` (`pages/menu-sede.js`).
- Delivery checkout re-checks each cart line against the sede's carta
  (`dish_not_on_carta`), like the sold-out re-check.
- `/pedir/{slug}` stays ONE link per org (PM 2026-09-21); the sede picked by
  GPS decides the carta through the diner session's `location_id`.

### Legacy notes
- `db_calculate_tips_by_attendance` and `db_calculate_payroll` respect `branch_id` via `ANY($n::int[])`.
- For operational staff: `restaurant_id` comes from the staff member's own DB record.

## Branch Hierarchy

- Matriz (head office): `parent_restaurant_id IS NULL`.
- Branch: `parent_restaurant_id` points to the Matriz.
- WhatsApp: branches use a `_b[TIMESTAMP]` suffix on `whatsapp_number` to avoid collisions.
- **Orgs with no WhatsApp (every self-serve signup) — 0096.** `restaurants.whatsapp_number` (the bot runtime's `bot_number`) falls back to `web<org_id>`: one key per org, shared by its sedes like a WhatsApp org's number; the sede is told apart by `location_id`. Before 0096 the table QR and `/pedir` 404'd for these orgs. Diner tests must also cover an org with NO number (`tests/test_self_serve_no_whatsapp.py`); every older seed sets one.
- **jsonb writes:** the app pool's codec (`database.init_connection`) serializes jsonb parameters, so never pass `json.dumps(x)` — that stores a JSON string. Test pools must use `init=init_connection`; ~29 older test files still don't (sweep pending).

## Wave 2 (Org/Location) — Post-deploy summary

**Applied to prod on 2026-04-18.** Current state and lessons learned are detailed in [docs/history/wave2_lessons.md](docs/history/wave2_lessons.md). Defensive queries for auditing drift are in [docs/history/wave2_monitoring_queries.md](docs/history/wave2_monitoring_queries.md). What matters for new code:

### Current schema state (summary, post-0038)

- **Canonical tables:** `organizations` (tenant) + `locations` (branch). `restaurants` is a read-only VIEW over `locations JOIN organizations`. The VIEW's `id` == `location_id`.
- **`org_id` + `location_id`** are the canonical columns. `org_id` is the tenant key.
- **`restaurant_id` column** — DROPPED from the 33 RLS tables in 0037.
- **RLS:** the `org_isolation` policy (by `org_id`) is active on 33 tables + FORCE RLS.
- **Auto-populate triggers:** DROPPED in 0037. App code must set `org_id` and `location_id` explicitly on INSERTs.
- **`location_id` nullable** on 17 operational tables (post-0054).
- Symbols dropped with forward-guard tests: `parent_restaurant_id`, `is_primary`, `restaurants_deprecated`, `_migration_restaurant_to_location`. See `tests/test_no_parent_restaurant_id_sql.py`, `tests/test_no_is_primary_sql.py`, `tests/test_no_branch_id_legacy_sql.py`.

### Mandatory pattern for new SQL post-Wave-2

- **Reads:** `FROM restaurants` STILL works (VIEW). Returns the same shape as before. `WHERE id = $1` is interpreted as filtering by `location_id`.
- **Writes to restaurants:** FORBIDDEN. Route UPDATE/INSERT/DELETE to the appropriate `organizations` + `locations`. Examples in `app/repositories/restaurant_repo.py` (`db_update_restaurant_fields`, `db_create_restaurant`).
- **Queries on operational tables:** use `org_id` (not `restaurant_id`). RLS filters via the `app.org_id` GUC.
- **`app.restaurant_id` GUC:** LEGACY (set for compat, do NOT use in new queries). Use `current_setting('app.org_id', true)`.
- **INSERTs on Location-level tables** (orders, staff, inventory, etc.): set `org_id` + `location_id` explicitly.
- **`features` as a dict:** can come back as a JSON string or a dict depending on driver/VIEW. Normalize with the `_features_dict()` helper.

### Environment variables

| Variable | Use |
|---|---|
| `DATABASE_URL` | Runtime app (postgres or mesio_app) |
| `PROD_DATABASE_URL` | Explicit alias for prod (rehearsal) |
| `TEST_DATABASE_URL` | Railway Test Postgres |
| `DATABASE_URL_ADMIN` | Superuser URL for migrations (falls back to DATABASE_URL if unset) |
| `ANTHROPIC_API_KEY` | Required by the bot + AI sim |
| `REDIS_URL` | Shared multi-worker state |
| `AI_SIM_ASSUME_YES=1` | Skip the sim's interactive prompt |
| `AI_SIM_ARGS` | Extra args for run_ai_sim.py |

**`REHEARSAL_MODE` and `AI_SIM_MODE` were removed from Railway**: if you reintroduce them to validate destructive pg_dump runs, remember to TURN THEM OFF afterward or prod stays down.

14 recurring errors already seen (Alembic varchar(32), `SET LOCAL ROLE` without a tx, pg_dump v17, multiple heads, `conn.execute(str)` SA 2.0, `:p::type` cast, etc.) and the main "strategic lesson" are in [docs/history/wave2_lessons.md](docs/history/wave2_lessons.md). Check that file before touching large migrations.
