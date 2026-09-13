# Multi-tenant: roles Postgres, RLS, Wave 2 org/location, sucursales

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Roles Postgres (Fase 1 RLS)

- **`postgres`** (superuser) — usado SOLO por Alembic vía `DATABASE_URL_ADMIN`. Bypass implícito de RLS.
- **`mesio_app`** (non-superuser, LOGIN) — conexión de la app en runtime. RLS se aplica normalmente. Tiene DML + sequences + execute + `mesio_superadmin` granted.
- **`mesio_superadmin`** (BYPASSRLS, NOINHERIT, no LOGIN) — activado via `SET LOCAL ROLE mesio_superadmin` dentro de `bypass_tenant_scope()`. Para rutas internas, scheduler leader tick, inbox worker pre-resolución, y analytics cross-tenant.

## Blindaje Multi-tenant RLS — Fase 1 Security Roadmap (v11.0)

**Objetivo:** imposible filtrar datos cross-tenant aunque un dev olvide `WHERE restaurant_id = $1`. El enforcement real vive en Postgres RLS; el Python es la plomería que alimenta el GUC.

### Estado actual: 100% aplicado

| Capa | Mecanismo | Archivo/migración |
|---|---|---|
| App fail-fast | `TenantNotSetError` si llamás `tenant_connection()` sin scope | `app/services/tenant_context.py` |
| App→DB | `SET LOCAL app.restaurant_id = $1` en cada `tenant_connection` | `app/services/tenant_db.py` |
| DB lectura | `USING (restaurant_id = NULLIF(current_setting('app.restaurant_id', true), '')::int)` | Alembic 0029 |
| DB escritura | `WITH CHECK (...)` — bloquea spoofing de restaurant_id en INSERT/UPDATE | Alembic 0029 |
| Owner lockdown | `ALTER TABLE ... FORCE ROW LEVEL SECURITY` | Alembic 0030 |
| Runtime non-superuser | App conecta como `mesio_app` (DML-only), RLS se aplica | `.env` + `alembic/env.py` |
| Admin escape hatch | `bypass_tenant_scope("reason")` → `SET LOCAL ROLE mesio_superadmin` (BYPASSRLS) | `app/services/tenant_context.py` |

**Prueba empírica (en la DB actual, probada en sesión 2026-04-15):**
```
mesio_app + scope=8  → orders=4   (solo ese tenant)
mesio_app + scope=19 → orders=11
mesio_app + no_scope → orders=0   (fail-closed)
INSERT sin scope     → InsufficientPrivilegeError (WITH CHECK dispara)
INSERT cross-tenant  → InsufficientPrivilegeError (scope=8 intentando restaurant_id=19)
bypass_tenant_scope  → orders=15 (todo)
```

### Modelo Wave-2: NO existe Matriz como entidad. Tampoco "primary".

Post-Wave-2 el schema canónico es `organizations` + `locations`. **Cada location es un peer del resto** — no hay "matriz", no hay "principal". Un org tiene N locations, todas equivalentes operacionalmente.

`locations.is_primary` existe en el schema **solo como scaffold de migración** (para mapear "qué location vieja era el matriz" durante el backfill 0034). **Es vestigial. NO usar en código nuevo.**

#### Reglas para código nuevo

1. **Enumerar negocios** → `db_get_all_orgs()` (devuelve org rows). NO `db_get_all_restaurants()` que filtra por `is_primary=true` y perpetúa el modelo viejo.
2. **Enumerar sedes de un negocio** → `db_get_org_locations(org_id)` (todas las locations, no solo "primary").
3. **NUNCA filtrar por `is_primary = true`** para encontrar "el restaurante principal". Esa pregunta no tiene sentido en el modelo nuevo. Si necesitás un default determinístico (ej. "primera location del org"), usar `ORDER BY id ASC LIMIT 1` — no es "la primary", es solo "una determinística".
4. **NUNCA depender de la "Matriz invariant"** (`org_id == matriz_location_id`). Solo es cierta para orgs migradas por 0034 (existentes al deploy de Wave-2). Para orgs creadas POST-deploy, `org_id` y `location_id` son enteros independientes (auto-incrementados por separado).
5. **NUNCA asumir que `restaurant.get("location_id") or org_id` es un fallback válido.** Es la "Matriz invariant trick" disfrazada — da el answer equivocado para orgs nuevas.
6. **Resolución correcta** de location_id cuando se necesita la sede:
   - Si el dict viene de `db_get_restaurant_by_id` o `db_get_all_restaurants` → usar `restaurant["location_id"]` (siempre populado post-Paso 7).
   - Si no se tiene un dict → query `SELECT id FROM locations WHERE org_id = $1 ORDER BY id ASC LIMIT 1` (cualquier sede, sin valor judgement de "primary").
7. **`parent_restaurant_id IS NULL` es legacy emulation.** El VIEW `restaurants` lo expone para backwards compat de código viejo. Para queries nuevas, usar `db_get_all_orgs()` directamente.
8. **`is_main_restaurant` parameter es vestigial.** No introducir en código nuevo.
9. **`X-Branch-ID` header SIEMPRE carga un `location_id`**. Si una ruta lo recibe, NO mezclar con `restaurant["id"]` (= org_id) — son dos integers distintos. Ver Paso 5 commits da08f5e + Paso 6 commit c442bba.

#### Deprecation status (vivo)

| Symbol | Status | Replacement |
|---|---|---|
| `db_get_all_restaurants()` | DEPRECATED for "list businesses" semantic | `db_get_all_orgs()` |
| `locations.is_primary` (column) | VESTIGIAL — only for migration backfill | (nothing — sedes are peers) |
| `parent_restaurant_id IS NULL` filter | LEGACY EMULATION (via VIEW) | `db_get_all_orgs()` |
| `is_main_restaurant` parameter | VESTIGIAL | (drop) |
| "Matriz invariant" fallback | REMOVED (Paso 7) | Explicit `restaurant["location_id"]` |
| `db_get_restaurant_by_id()` | **DELETED 2026-09-11** — aceptaba location_id U org_id y en colisión devolvía OTRO restaurante (P0 cross-tenant) | `db_get_restaurant_by_location_id()` / `db_get_restaurant_by_org_id()` — elegir por intención, nunca adivinar. Guardia: `tests/test_no_ambiguous_restaurant_lookup.py` |
| `users.branch_id` | AMBIGUO (sin FK; unos writers guardaban org_id, otros location_id) | `users.org_id` + `users.location_id` (migración 0081, con FK). Auth DENIEGA si `org_id` no se pudo resolver — nunca fallback |
| `db_update_restaurant_fields()` / `db_update_subscription()` y `POST /api/internal/admin/update-restaurant` + `/set-subscription` | **DELETED 2026-09-12** — resolvían la organización por subconsulta de sede; con colisión de ids escribían sobre OTRO cliente | `PATCH /api/internal/admin/organizations/{org_id}`. Guardia: `tests/test_no_legacy_restaurant_field_writes.py` |

### Patrón de uso

**En rutas FastAPI (admin/staff autenticado):**
```python
# Restaurante admin (owner/gerente) — scope desde el dict del restaurante
@router.get("/api/loyalty/balance")
async def get_balance(
    phone: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),  # ← yield-based, entra en tenant_scope
):
    return await db.db_get_loyalty_balance(restaurant["id"], phone)

# Staff autenticado con JWT "staff:<uuid>" — scope desde user["restaurant_id"]
@router.get("/api/staff/self/timecard")
async def my_timecard(user: dict = Depends(get_current_user_scoped)):
    return await db.db_get_staff_timecard_rows(user["restaurant_id"])
```

**En bot runtime (webhook Meta):**
```python
# inbox_worker._handle_meta_whatsapp — después de resolver restaurant desde bot_number
if _tenant_id is not None:
    with tenant_scope(_tenant_id):
        await _process_message(...)
```

**En rutas/servicios cross-tenant (internal, scheduler, analytics):**
```python
# app/routes/internal/admin.py — superadmin Mesio
with bypass_tenant_scope("internal_admin_restaurants_list"):
    return await db.db_get_all_restaurants()

# scheduler leader tick — enumera restaurantes, luego scope por cada uno
with bypass_tenant_scope("scheduler_leader_tick"):
    restaurants = await db_get_all_restaurants()
    for r in restaurants:
        with tenant_scope(r["id"]):
            await _per_restaurant_task(r)

# chat.py webhook Meta — enqueue es pre-tenant
with bypass_tenant_scope("webhook_enqueue_cross_tenant"):
    await inbox_repo.enqueue(...)
```

### Clasificación de repos

| Repo | Tipo | Notas |
|---|---|---|
| `loyalty_repo`, `fiscal_repo`, `discounts_repo`, `customer_profiles_repo` | Tenant-scoped 100% | `_get_pool` eliminado |
| `orders_repo`, `conversations_repo`, `inventory_repo`, `reviews_repo`, `reservations_repo`, `reservation_deposits_repo`, `weekly_reports_repo`, `menu_analytics_repo` | Tenant-scoped 100% | `_get_pool` eliminado |
| `staff_repo`, `tables_repo` | Tenant-scoped con `bypass_tenant_scope` interno en ~20 funciones | 🚧 deuda: auditar cada bypass interno (kiosco público vs. cuestionables) |
| `marketing_repo` | MIXED: `marketing_messages_log` tenant; `prospects`/CRM GLOBAL | Mantiene `_get_pool` para GLOBAL |
| `restaurant_repo` | MIXED: 14 tenant (config por restaurant_id), 38 GLOBAL (users, enumeración, pre-resolución bot) | Mantiene `_get_pool` para GLOBAL |
| `sessions_repo` | GLOBAL | `sessions` no tiene `restaurant_id` (auth cross-tenant). NO MIGRAR. |
| `inbox_repo` | GLOBAL | `webhook_inbox` es pre-resolución por diseño. NO MIGRAR. |
| `crm_repo` (`app/repositories/internal/`) | GLOBAL | Herramientas internas Mesio. NO MIGRAR. |

### Reglas que cualquier cambio futuro DEBE respetar

1. **Nunca uses `get_pool()` / `pool.acquire()` directo en código nuevo de repos.** Usá `tenant_connection()` + `tenant_scope(rid)` en el call site.
2. **Nunca catches `TenantNotSetError`.** Es la señal de diseño — si salta, hay un call site sin scope.
3. **Toda tabla nueva con `restaurant_id NOT NULL` DEBE agregarse a `_RLS_TABLES` en una nueva migración** que habilite RLS + FORCE. Si la olvidás, la tabla queda sin protección.
4. **Los parámetros de `set_config` son posicionales, nunca f-string.** `SET LOCAL` vía `SELECT set_config('app.restaurant_id', $1, true)`.
5. **Migraciones Alembic corren con `DATABASE_URL_ADMIN` (superuser).** La app runtime JAMÁS debe apuntarse a una URL superuser.
6. **Tests nuevos mockean `app.services.database.get_pool`** + wrappean en `tenant_scope(N)`. El patrón viejo (`monkeypatch.setattr(repo, "_get_pool", ...)`) rompe porque `_get_pool` se eliminó de los repos migrados. Referencia: `tests/test_loyalty_repo_tenant.py`.
7. **`bypass_tenant_scope` SIEMPRE con reason ≥ 8 chars.** Se loguea para auditoría. Reservado para: rutas `/api/internal/*`, scheduler leader tick, inbox worker pre-resolución, agent.py lookups cross-tenant pre-scope, endpoints de kiosco público (WebAuthn).

### Deuda pendiente (no bloqueante)

- Auditar los ~20 `bypass_tenant_scope` internos en `staff_repo.py` — varios son cuestionables (breaks, self-profile) y se pueden apretar a `tenant_connection()` si el call site siempre entra con scope.
- ~~17 integration tests + 26 más en otros archivos~~ — **CERRADO 2026-04-19**: refactorizados con `_ConnProxy` pattern (workaround `asyncpg.Connection.__slots__`) + INSERTs vía `organizations + locations` (no más `restaurants` VIEW write) + columnas `org_id` (no más `restaurant_id`). 46 tests passing post-refactor contra TEST_DATABASE_URL.
- ~~6 X-Branch-ID conflation sites pendientes en `staff.py`~~ — **CERRADO en Paso 10**. Los 6 sitios fueron migrados al fix template: org_id consistente para queries org-level, location_id propagado al param opcional `branch_id` de `db_calculate_payroll` para tip scoping per-sede.
- Fase 2 (integridad/concurrencia) y Fase 3 (desacoplamiento IA + middlewares) — ✅ shipped 2026-04-17. El plan original se eliminó del repo cuando se cerró el último item.

## Contexto Multi-Sucursal

- Header `X-Branch-ID` dicta qué datos leer. Si es `"all"`, retornar Matriz + Sucursales.
- `get_current_restaurant` en `deps.py` resuelve el restaurante del token JWT admin.
- Para staff operativo: `restaurant_id` viene del propio registro de staff en BD.
- `db_calculate_tips_by_attendance` y `db_calculate_payroll` respetan `branch_id` via `ANY($n::int[])`.

## Jerarquía de Sucursales

- Matriz: `parent_restaurant_id IS NULL`.
- Sucursal: `parent_restaurant_id` apunta a la Matriz.
- WhatsApp: sucursales usan sufijo `_b[TIMESTAMP]` en `whatsapp_number` para evitar colisiones.

## Wave 2 (Org/Location) — Resumen post-deploy

**Aplicado a prod 2026-04-18.** Estado actual y aprendizajes detallados en [docs/history/wave2_lessons.md](docs/history/wave2_lessons.md). Queries defensivas para auditar drift en [docs/history/wave2_monitoring_queries.md](docs/history/wave2_monitoring_queries.md). Lo importante para código nuevo:

### Estado actual del schema (resumen, post-0038)

- **Tablas canónicas:** `organizations` (tenant) + `locations` (sede). `restaurants` es VIEW read-only sobre `locations JOIN organizations`. `id` de la VIEW == `location_id`.
- **`org_id` + `location_id`** son las columnas canónicas. `org_id` es el tenant key.
- **`restaurant_id` column** — DROPPED de las 33 tablas RLS en 0037.
- **RLS:** policy `org_isolation` (por `org_id`) activa en 33 tablas + FORCE RLS.
- **Triggers auto-populate:** DROPPED en 0037. App code debe setear `org_id` y `location_id` explícitamente en INSERTs.
- **`location_id` nullable** en 17 tablas operativas (post-0054).
- Symbols dropeados con forward-guard tests: `parent_restaurant_id`, `is_primary`, `restaurants_deprecated`, `_migration_restaurant_to_location`. Ver `tests/test_no_parent_restaurant_id_sql.py`, `tests/test_no_is_primary_sql.py`, `tests/test_no_branch_id_legacy_sql.py`.

### Patrón obligatorio post-Wave-2 para SQL nuevo

- **Reads:** `FROM restaurants` SIGUE funcionando (VIEW). Retorna shape idéntico al viejo. `WHERE id = $1` se interpreta como filtrado por `location_id`.
- **Writes a restaurants:** PROHIBIDAS. Routear UPDATE/INSERT/DELETE al `organizations` + `locations` apropiado. Ejemplos en `app/repositories/restaurant_repo.py` (`db_update_restaurant_fields`, `db_create_restaurant`).
- **Queries en tablas operativas:** usar `org_id` (no `restaurant_id`). RLS filtra via `app.org_id` GUC.
- **`app.restaurant_id` GUC:** LEGACY (seteado por compat, NO usar en queries nuevas). Usar `current_setting('app.org_id', true)`.
- **INSERTs en tablas Location-level** (orders, staff, inventory, etc.): setear `org_id` + `location_id` explícitamente.
- **`features` como dict:** puede venir como str JSON o dict según driver/VIEW. Normalizar con helper `_features_dict()`.

### Variables de entorno

| Variable | Uso |
|---|---|
| `DATABASE_URL` | Runtime app (postgres o mesio_app) |
| `PROD_DATABASE_URL` | Alias explícito para prod (rehearsal) |
| `TEST_DATABASE_URL` | Postgres Test de Railway |
| `DATABASE_URL_ADMIN` | Superuser URL para migraciones (cae a DATABASE_URL si no se setea) |
| `ANTHROPIC_API_KEY` | Requerido por bot + AI sim |
| `REDIS_URL` | Estado compartido multi-worker |
| `AI_SIM_ASSUME_YES=1` | Skip prompt interactivo del sim |
| `AI_SIM_ARGS` | Args extra para run_ai_sim.py |

**`REHEARSAL_MODE` y `AI_SIM_MODE` fueron removidos de Railway**: si los reintroducís para validar destructiva via pg_dump, recordá DESACTIVARLOS después o prod queda caído.

14 errores recurrentes ya vistos (Alembic varchar(32), `SET LOCAL ROLE` sin tx, pg_dump v17, multiple heads, `conn.execute(str)` SA 2.0, `:p::tipo` cast, etc.) y la "lección estratégica principal" están en [docs/history/wave2_lessons.md](docs/history/wave2_lessons.md). Consultá ese archivo antes de tocar migraciones grandes.

