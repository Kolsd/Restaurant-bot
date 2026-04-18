# Org/Location Migration Plan — Eliminar la Matriz

**Autor:** Claude Opus 4.7 — diseño arquitectónico
**Fecha:** 2026-04-17
**Estado:** PLAN (no ejecutado)
**Scope:** Reemplazar el modelo Matriz/Sucursal por **Organization + Location** uniforme, manteniendo RLS multi-tenant y zero-downtime.

---

## 1. Problema y diseño (resumen ejecutivo)

Hoy la tabla `restaurants` hace doble papel: es contenedor de tenant (billing, menú canónico, users, subscription) **Y** es un restaurante operativo con WhatsApp, staff, pedidos. La "Matriz" (`parent_restaurant_id IS NULL`) es tratada especial por el bot, por `deps.py`, por el scheduler y por el frontend. Eso genera:

- Fallback raro `whatsapp_number` a la Matriz cuando la sucursal tiene NULL
- Sufijo `_b[TIMESTAMP]` en `whatsapp_number` para evitar colisiones
- Edge cases "Matriz que vende" vs "Matriz que solo agrupa"
- `branch_id` que a veces es `restaurants.id` de Matriz, a veces de sucursal
- Menú duplicado por `db_sync_menu_to_branches` (hack de propagación)

**Solución:** separar en dos tablas con roles claros.

| Concepto | Tabla | Propósito |
|---|---|---|
| **Organization** | `organizations` | Tenant. Billing, subscription, users admin, menú canónico, WhatsApp default, `features`. Uno por cliente Mesio. |
| **Location** | `locations` | Sede operativa. Dirección, GPS, WhatsApp override opcional, staff, inventario, pedidos. Uno o N por Org. |

**Tres ejes de diseño (ya decididos):**

1. **Teléfono**: vive en Org por default. Location puede override opcional para cadenas con N números. El routing del webhook pasa de `bot_number → restaurant` a `bot_number → org`.
2. **Binding de Location en conversación**: `location_id NULL` al inicio. Se resuelve según canal:
   - QR de mesa → Mensaje 1 (QR embebe `location_id`)
   - Pickup → Al elegir/confirmar sucursal
   - Delivery → Al dar dirección (GPS triangulation ya existe)
   - Chat exploratorio → nunca hasta que intente pedir
3. **Menú**: catálogo canónico en Org (`organizations.menu` JSONB). Availability/stock per-Location (ya existe `menu_availability`, solo renombra columna).

**RLS:** el GUC pasa de `app.restaurant_id` a `app.org_id`. Tablas operativas (orders, staff, inventory, etc.) mantienen `org_id` (para RLS) **y** `location_id` (para routing operativo).

---

## 2. Schema objetivo

### 2.1 Tablas nuevas

```sql
CREATE TABLE organizations (
    id                     BIGSERIAL PRIMARY KEY,
    name                   TEXT NOT NULL,
    slug                   TEXT UNIQUE,
    whatsapp_number        TEXT UNIQUE,          -- default Org-level, NULL permitido para cadenas
    wa_phone_id            TEXT,
    wa_access_token        TEXT,
    menu                   JSONB NOT NULL DEFAULT '[]'::jsonb,
    features               JSONB NOT NULL DEFAULT '{}'::jsonb,
    subscription_plan      TEXT DEFAULT 'free',
    subscription_status    TEXT DEFAULT 'active',
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX ux_organizations_whatsapp ON organizations(whatsapp_number)
    WHERE whatsapp_number IS NOT NULL;

CREATE TABLE locations (
    id                     BIGSERIAL PRIMARY KEY,
    org_id                 BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name                   TEXT NOT NULL,
    code                   TEXT,                 -- opcional: "centro", "norte", "sede-01"
    address                TEXT,
    latitude               DOUBLE PRECISION,
    longitude              DOUBLE PRECISION,
    whatsapp_number        TEXT,                 -- override opcional; null = usa el de la Org
    wa_phone_id            TEXT,                 -- si override, necesita su propio phone_id
    wa_access_token        TEXT,                 -- si override, necesita su propio token
    active                 BOOLEAN NOT NULL DEFAULT true,
    is_primary             BOOLEAN NOT NULL DEFAULT false,  -- "sede principal" — solo 1 por Org
    timezone               TEXT DEFAULT 'America/Bogota',
    opening_hours          JSONB DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX ux_locations_whatsapp ON locations(whatsapp_number)
    WHERE whatsapp_number IS NOT NULL;
CREATE UNIQUE INDEX ux_locations_org_primary ON locations(org_id) WHERE is_primary = true;
CREATE INDEX ix_locations_org ON locations(org_id);
```

**Invariante clave:** cada Org tiene exactamente **una** `is_primary = true` Location (la sede principal). Se enforce con el unique partial index.

### 2.2 Columnas agregadas a tablas existentes

Dos reglas:
- **Tablas Org-level** (catálogo, carts pre-resolución, conversaciones, billing, users): agregar `org_id`, dropear `restaurant_id`.
- **Tablas Location-level** (orders, staff, inventario, fiscal, mesas): agregar `org_id` **y** `location_id`. Eventualmente dropear `restaurant_id` (se mantiene durante la migración como alias).

### 2.3 Clasificación completa de las 33 tablas RLS

| Tabla | Nivel | `org_id` | `location_id` | Nota |
|---|---|---|---|---|
| `attendance_deductions` | Location | ✅ | ✅ | Deducción en sede específica |
| `billing_log` | Org | ✅ | ❌ | Factura del plan Mesio |
| `carts` | Org (hasta checkout) | ✅ | 🟡 nullable | `location_id` se resuelve al hacer order |
| `contract_templates` | Org | ✅ | ❌ | Templates compartidos |
| `conversations` | Org | ✅ | 🟡 nullable | Location se resuelve según canal |
| `customer_profiles` | Org | ✅ | ❌ | El cliente es de la marca, no de la sede |
| `dish_recipes` | Org | ✅ | ❌ | Receta canónica |
| `fiscal_invoices` | Location | ✅ | ✅ | DIAN por sede (resolución fiscal propia) |
| `fiscal_resolution` | Location | ✅ | ✅ | Resolución DIAN por sede |
| `inventory` | Location | ✅ | ✅ | Stock por sede |
| `loyalty_customers` | Org | ✅ | ❌ | Puntos acumulan entre sedes |
| `loyalty_ledger` | Org | ✅ | 🟡 nullable | Registra `earned_at_location` opcional |
| `marketing_messages_log` | Org | ✅ | ❌ | Campañas Org-wide |
| `menu_availability` | Location | ✅ | ✅ | **Por-sede** (ya existe per-restaurant; rename `restaurant_id → location_id`) |
| `menu_events` | Org | ✅ | 🟡 nullable | Cambios de menú (global) o de disponibilidad (sede) |
| `nps_responses` | Org | ✅ | 🟡 nullable | Si la conversación tenía Location, se registra |
| `nps_waiting` | Org | ✅ | 🟡 nullable | |
| `occupancy_snapshots` | Location | ✅ | ✅ | Snapshot de ocupación por sede |
| `orders` | Location | ✅ | ✅ | Pedido siempre ejecutado en una sede |
| `overtime_requests` | Location | ✅ | ✅ | Overtime del staff de una sede |
| `payroll_runs` | Org | ✅ | 🟡 nullable | Run puede cubrir Org completa o una sede |
| `staff` | Location | ✅ | ✅ | Staff asignado a una sede (puede tener N filas si trabaja en varias) |
| `staff_deduction_items` | Location | ✅ | ✅ | |
| `staff_schedules` | Location | ✅ | ✅ | |
| `staff_shifts` | Location | ✅ | ✅ | |
| `subscription_usage` | Org | ✅ | ❌ | Contador de uso del plan |
| `table_orders` | Location | ✅ | ✅ | Mesa siempre en una sede |
| `table_sessions` | Location | ✅ | ✅ | |
| `time_slot_discounts` | Location | ✅ | ✅ | Yield management por sede |
| `tip_distributions` | Location | ✅ | ✅ | Propinas por sede |
| `waiter_alerts` | Location | ✅ | ✅ | |
| `webauthn_challenges` | Location | ✅ | ✅ | Kiosco biométrico por sede |
| `weekly_reports` | Org | ✅ | 🟡 nullable | Reporte Org-wide o por sede |

**Tablas agregadas fuera del set RLS actual:** `restaurant_tables` (ya tenía `branch_id`, renombra a `location_id`), `reservations` (ya tenía `branch_id`), `reservation_deposits`, `webauthn_credentials` (via staff FK).

### 2.4 Nuevo RLS — dos políticas

**Política Org (single-tier)**, aplicada a tablas Org-level y a todas:
```sql
CREATE POLICY org_isolation ON <tabla>
    USING (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
    WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint);
```

**No hay RLS por Location.** El `location_id` es un parámetro operativo, no una frontera de seguridad. Si quisiéramos evitar que un manager de una sede lea datos de otra sede de la misma Org, se hace en app layer (filtros explícitos en queries), no en RLS. Motivo: un owner de la Org legítimamente necesita ver todas las sedes, y RLS por Location exigiría un segundo GUC + cambios en policies.

### 2.5 Nuevo `tenant_scope` y GUC

```python
# app/services/tenant_context.py
@contextmanager
def tenant_scope(org_id: int) -> Generator[int, None, None]:
    """Pin the current async context to *org_id* (was: restaurant_id)."""
    ...

# app/services/tenant_db.py
await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
```

Rename semántico: `restaurant_id` → `org_id` en todo el stack de tenant_*. `location_id` viaja como **parámetro explícito** en signatures de repos operativos, no via GUC.

---

## 3. Migración — plan en 7 fases (7 migrations Alembic + code)

Cada fase es una migration Alembic individual, idempotente (`IF NOT EXISTS`), y reversible con `downgrade()`. Entre fases la app puede seguir funcionando (compatible hacia atrás).

### Fase 0 — Preparación (sin migration)
- Crear branch `org-location-migration`
- Snapshot de producción con `pg_dump` etiquetado
- Habilitar `DATABASE_URL_ADMIN` en staging
- Correr tests actuales: `pytest tests/ --ignore=tests/ai_sim` → 766/766 debe pasar. Baseline.

### Fase 1 — `0034_create_organizations_locations.py`

**DDL:**
- Crear tabla `organizations` (vacía)
- Crear tabla `locations` (vacía)
- Unique/indexes mencionados en §2.1
- NO tocar `restaurants` todavía

**Data (en la misma migration, con `op.execute`):**

```sql
-- Paso 1: cada Matriz (parent_restaurant_id IS NULL) se convierte en 1 Org
INSERT INTO organizations (
    id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
    menu, features, created_at
)
SELECT id, name, slug, whatsapp_number, wa_phone_id, wa_access_token,
       menu, features, created_at
FROM restaurants
WHERE parent_restaurant_id IS NULL;

-- Paso 2: asegurar que el serial de organizations arranque después del max
SELECT setval('organizations_id_seq',
              (SELECT COALESCE(MAX(id), 1) FROM organizations));

-- Paso 3: toda Matriz crea su propia Location "principal"
INSERT INTO locations (
    org_id, name, code, address, latitude, longitude,
    whatsapp_number, wa_phone_id, wa_access_token,
    active, is_primary, timezone, created_at
)
SELECT id AS org_id,
       name,
       'principal' AS code,
       address, latitude, longitude,
       NULL AS whatsapp_number,   -- hereda del Org (no duplicar)
       NULL AS wa_phone_id,
       NULL AS wa_access_token,
       true AS active,
       true AS is_primary,
       COALESCE((features->>'timezone')::text, 'America/Bogota'),
       created_at
FROM restaurants
WHERE parent_restaurant_id IS NULL;

-- Paso 4: cada sucursal se convierte en Location
INSERT INTO locations (
    org_id, name, code, address, latitude, longitude,
    whatsapp_number, wa_phone_id, wa_access_token,
    active, is_primary, timezone, created_at
)
SELECT parent_restaurant_id AS org_id,
       name,
       lower(regexp_replace(name, '\s+', '-', 'g')) AS code,
       address, latitude, longitude,
       -- si el whatsapp_number tiene sufijo _b[TS] o es duplicado del parent → NULL
       CASE
         WHEN whatsapp_number LIKE '%\_b%' ESCAPE '\' THEN NULL
         WHEN whatsapp_number = (SELECT whatsapp_number FROM restaurants p
                                 WHERE p.id = restaurants.parent_restaurant_id) THEN NULL
         ELSE whatsapp_number
       END,
       CASE WHEN whatsapp_number LIKE '%\_b%' ESCAPE '\' THEN NULL ELSE wa_phone_id END,
       CASE WHEN whatsapp_number LIKE '%\_b%' ESCAPE '\' THEN NULL ELSE wa_access_token END,
       true AS active,
       false AS is_primary,
       'America/Bogota',
       created_at
FROM restaurants
WHERE parent_restaurant_id IS NOT NULL;
```

**Tabla de mapping (temporal, para fase 2):**

```sql
CREATE TABLE _migration_restaurant_to_location (
    old_restaurant_id BIGINT PRIMARY KEY,
    org_id            BIGINT NOT NULL,
    location_id       BIGINT NOT NULL
);

INSERT INTO _migration_restaurant_to_location
    SELECT r.id,
           COALESCE(r.parent_restaurant_id, r.id) AS org_id,
           l.id AS location_id
    FROM restaurants r
    JOIN locations l ON l.org_id = COALESCE(r.parent_restaurant_id, r.id)
                    AND ((r.parent_restaurant_id IS NULL AND l.is_primary)
                      OR l.name = r.name);
```

**Validación obligatoria en la misma migration:**

```sql
DO $$
DECLARE orphans INT;
BEGIN
    SELECT COUNT(*) INTO orphans FROM restaurants r
    LEFT JOIN _migration_restaurant_to_location m ON m.old_restaurant_id = r.id
    WHERE m.old_restaurant_id IS NULL;
    IF orphans > 0 THEN
        RAISE EXCEPTION 'Migration 0034: % restaurants sin mapping a location', orphans;
    END IF;
END $$;
```

**Sin cambio en código aún.** La app sigue usando `restaurants`.

### Fase 2 — `0035_add_org_location_columns.py`

Para cada una de las 33 tablas RLS + `restaurant_tables` + `reservations` + `reservation_deposits`:

```sql
ALTER TABLE <tabla> ADD COLUMN IF NOT EXISTS org_id BIGINT;
-- Solo para tablas Location-level:
ALTER TABLE <tabla> ADD COLUMN IF NOT EXISTS location_id BIGINT;
```

**Backfill batched** (10k rows por batch, commit intermedio para evitar lock escalation):

```sql
-- Org-level tables
UPDATE <tabla> t
SET org_id = m.org_id
FROM _migration_restaurant_to_location m
WHERE t.restaurant_id = m.old_restaurant_id AND t.org_id IS NULL;

-- Location-level tables
UPDATE <tabla> t
SET org_id = m.org_id, location_id = m.location_id
FROM _migration_restaurant_to_location m
WHERE t.restaurant_id = m.old_restaurant_id AND t.org_id IS NULL;
```

**Caso especial — tablas con `branch_id` existente** (orders, table_orders, conversations, nps_responses, table_sessions, carts, waiter_alerts, nps_waiting, reservations):

Estas ya tienen `branch_id` apuntando a una sucursal real (o a la Matriz si es single-location). La regla:
- `location_id = m.location_id` donde `m.old_restaurant_id = branch_id` (si branch_id no-NULL)
- Fallback: `location_id = m.location_id` donde `m.old_restaurant_id = restaurant_id` (si branch_id NULL)

```sql
UPDATE orders o
SET org_id = COALESCE(mb.org_id, mr.org_id),
    location_id = COALESCE(mb.location_id, mr.location_id)
FROM _migration_restaurant_to_location mr
LEFT JOIN _migration_restaurant_to_location mb ON mb.old_restaurant_id = o.branch_id
WHERE o.restaurant_id = mr.old_restaurant_id AND o.org_id IS NULL;
```

**NOT NULL + FK + indexes:**

```sql
-- Después del backfill
ALTER TABLE <tabla> ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE <tabla> ADD CONSTRAINT fk_<tabla>_org
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
CREATE INDEX ix_<tabla>_org ON <tabla>(org_id);

-- Location-level
ALTER TABLE <tabla> ALTER COLUMN location_id SET NOT NULL;  -- EXCEPTO carts, conversations, nps_*, loyalty_ledger, payroll_runs, menu_events, weekly_reports (nullable por diseño)
ALTER TABLE <tabla> ADD CONSTRAINT fk_<tabla>_location
    FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE;
CREATE INDEX ix_<tabla>_location ON <tabla>(location_id);
```

**Validación:**

```sql
DO $$
DECLARE bad INT;
BEGIN
    SELECT COUNT(*) INTO bad FROM orders WHERE org_id IS NULL;
    IF bad > 0 THEN RAISE EXCEPTION 'orders con org_id NULL: %', bad; END IF;
    -- Repetir para cada tabla Location-level con NOT NULL
END $$;
```

**Aún no se drope `restaurant_id`** — sigue poblándose por la app vieja.

### Fase 3 — `0036_dual_rls_policies.py`

Agregar política nueva sin dropear la vieja. Durante esta fase corren las dos en paralelo:

```sql
-- Nueva policy (RLS por org_id)
CREATE POLICY org_isolation ON <tabla>
    USING (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint)
    WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::bigint);

-- La policy vieja (tenant_isolation por restaurant_id) se mantiene temporalmente
```

Postgres evalúa policies con OR (si hay múltiples), así que una query pasa si cumple **cualquiera**. Esto permite que código viejo (`SET app.restaurant_id`) y código nuevo (`SET app.org_id`) convivan durante el cutover de app.

### Fase 4 — Deploy code wave 1 (lectura dual, escritura vieja)

**No hay migration Alembic en esta fase, solo código.** Se despliega con código que:
- **Escribe** seteando **ambos** GUC: `app.restaurant_id` Y `app.org_id`
- **Lee** vía el GUC nuevo (`app.org_id`) — transparente por RLS
- **Continúa poblando** `restaurant_id` en inserts (backward-compat)
- **Pobla también** `org_id` y `location_id` en inserts nuevos

**Archivos tocados en esta wave (resumen; detalle en §4):**
- `app/services/tenant_db.py` — setea ambos GUC
- `app/services/tenant_context.py` — helper dual `tenant_scope_dual(org_id, restaurant_id)`
- `app/repositories/*` — inserts agregan `org_id, location_id` a los INSERT
- `app/services/agent.py`, `app/services/inbox_worker.py` — resuelven `(org_id, location_id)` en lugar de `restaurant_id` solo

**Ejecutar en staging, correr `run_ai_sim.py` + suite completa.** Verificar 0 errores durante ~24h con tráfico real.

### Fase 5 — `0037_drop_legacy_rls_and_restaurant_id.py`

Una vez confirmada la estabilidad:

```sql
-- Dropear policy vieja
DROP POLICY IF EXISTS tenant_isolation ON <tabla>;

-- Dropear columna restaurant_id donde no se necesita
-- (Tablas Org-level: ya tienen org_id que la reemplaza)
ALTER TABLE conversations     DROP COLUMN IF EXISTS restaurant_id;
ALTER TABLE carts             DROP COLUMN IF EXISTS restaurant_id;
-- ... etc para las 13 tablas Org-level
```

**Tablas Location-level**: renombrar `restaurant_id → location_id_legacy` durante una ventana, luego dropear. En la práctica: dropear directo porque `location_id` ya existe y está poblada desde Fase 2.

```sql
ALTER TABLE orders           DROP COLUMN IF EXISTS restaurant_id;
ALTER TABLE staff            DROP COLUMN IF EXISTS restaurant_id;
-- ... etc para las 20 tablas Location-level
```

**`restaurants` se mantiene como VIEW temporal** para no romper superadmin/analytics que aún no estén migrados:

```sql
ALTER TABLE restaurants RENAME TO restaurants_deprecated;

CREATE VIEW restaurants AS
SELECT
    l.id,
    CASE WHEN l.is_primary THEN NULL ELSE l.org_id END AS parent_restaurant_id,
    COALESCE(o.name, l.name) AS name,
    l.name AS location_name,
    COALESCE(l.whatsapp_number, o.whatsapp_number) AS whatsapp_number,
    l.address,
    l.latitude,
    l.longitude,
    o.menu,
    o.features,
    COALESCE(l.wa_phone_id, o.wa_phone_id) AS wa_phone_id,
    COALESCE(l.wa_access_token, o.wa_access_token) AS wa_access_token,
    o.slug,
    l.created_at,
    l.updated_at
FROM locations l
JOIN organizations o ON o.id = l.org_id;
```

Esta view es READ-ONLY. Código viejo que aún hiciera `SELECT * FROM restaurants` sigue funcionando. Inserts/updates fallan — forzando migración del código restante.

### Fase 6 — Deploy code wave 2 (cleanup)

- Dropear el GUC dual: solo `app.org_id`
- Dropear `restaurant_id` de signatures internas
- Eliminar `restaurants_deprecated` cuando el grep en logs muestre 0 hits durante 2 semanas
- Actualizar `CLAUDE.md` con nuevo modelo

### Fase 7 — `0038_drop_legacy_restaurants.py` (opcional, 2-4 semanas después)

```sql
DROP VIEW restaurants;
DROP TABLE restaurants_deprecated;
DROP TABLE _migration_restaurant_to_location;
```

---

## 4. Cambios de código por área

### 4.1 Auth / JWT (`app/services/auth.py`, `app/routes/deps.py`)

**Claim del JWT:** hoy `sub` = username (admin) o `staff:<uuid>` (staff). **No cambiar formato** — la resolución a `(org_id, location_id)` se hace via DB lookup como hoy.

Cambios:
- `login()`: en lugar de retornar `restaurant.id`, retorna `org_id` + `location_id` por default (la `is_primary` de la Org, o la Location del staff si aplica).
- Respuesta JSON al frontend cambia:
  ```json
  {
    "token": "...",
    "org": { "id": 1, "name": "...", "whatsapp_number": "...", "features": {...} },
    "locations": [
      { "id": 10, "name": "Sede Norte", "is_primary": true, "whatsapp_number": null },
      { "id": 11, "name": "Sede Sur",   "is_primary": false, "whatsapp_number": "..." }
    ],
    "default_location_id": 10,
    "role": "owner"
  }
  ```
- `get_current_restaurant` → **`get_current_org`** + **`get_current_location`** (nullable, se resuelve de header `X-Location-ID`).
- `get_current_restaurant_scoped` → **`get_current_org_scoped`** (entra en `tenant_scope(org_id)`).
- **Deprecated alias** durante migración: `get_current_restaurant` retorna `{"id": org_id, "name": org_name, ...}` mapeando Org → shape de restaurant. Cuando todos los call sites migran, se elimina.

### 4.2 Tenant plumbing (`tenant_context.py`, `tenant_db.py`)

```python
# tenant_context.py — signature cambia
_current_org: ContextVar[int | None] = ContextVar("current_org", default=None)

def current_org_id() -> int: ...           # renamed from current_tenant_id
def tenant_scope(org_id: int): ...         # renamed arg, mismo contrato
def bypass_tenant_scope(reason: str): ...  # sin cambio

# Durante Wave 1 (Fase 4), agregar:
@contextmanager
def tenant_scope_dual(org_id: int):
    """Setea ambos GUC: app.org_id (nuevo) y app.restaurant_id (legacy).
    Eliminado en Wave 2 (Fase 6)."""
    token = _current_org.set(org_id)
    try:
        yield org_id
    finally:
        _current_org.reset(token)

# tenant_db.py — dual set durante Wave 1
async def tenant_connection():
    ...
    await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
    await conn.execute("SELECT set_config('app.restaurant_id', $1, true)", str(org_id))  # legacy
```

### 4.3 Webhook routing (`app/routes/chat.py`, `app/services/inbox_worker.py`)

Hoy:
```python
# chat.py enqueue (sin tenant)
await inbox_repo.enqueue(...)

# inbox_worker._handle_meta_whatsapp
r = await db_get_restaurant_by_phone(bot_number)
with tenant_scope(r["id"]):
    await _process_message(...)
```

Después:
```python
# chat.py enqueue — igual, pre-resolución
await inbox_repo.enqueue(...)

# inbox_worker._handle_meta_whatsapp
org = await db_get_org_by_phone(bot_number)           # NUEVO repo fn
# Location: si el webhook viene de un QR, el wa.messages[0].context o el texto embeben location_id.
# Si no, location_id = None y se resuelve más tarde en el agent.
location_id = _extract_location_from_payload(payload) or None
with tenant_scope(org["id"]):
    await _process_message(..., location_id=location_id)
```

**Nuevos repos:**
- `db_get_org_by_phone(phone)` — busca en `organizations.whatsapp_number` Y `locations.whatsapp_number`. Si match en Location, retorna `(org, location)` — pre-bound.
- `db_get_org_locations(org_id)` — lista de Locations activas.
- `db_get_primary_location(org_id)` — la is_primary.
- `db_resolve_location_by_gps(org_id, lat, lon, radius_km=5)` — triangulación, reemplaza la lógica actual en agent.py.

### 4.4 Agent (`app/services/agent.py`)

**Estado de conversación** pasa a incluir `location_id` nullable:

```python
# conversations table: ya tiene location_id NULL (Fase 2)
# El agent lee/escribe conversation.location_id junto con el resto del estado
```

**Tool calls afectados:**

| Tool | Antes | Después |
|---|---|---|
| `create_delivery_order` | recibía `branch_id` via triangulación implícita | Recibe `location_id`; si None, el tool resuelve por GPS y escribe de vuelta |
| `create_pickup_order` | asumía una sola sede o pedía elegir | Pide elegir Location si Org tiene >1; pre-selecciona si solo hay 1 |
| `place_order` (mesa) | `location_id` ya viene del QR via `table_context` | Sin cambio funcional, solo renombrar |
| `get_menu` / `find_dish` | lee menú de `restaurants.menu` | Lee de `organizations.menu`. Si `location_id` presente, filtra por `menu_availability` |

**`_resolve_branch_id(table_context)` → `_resolve_location_id(table_context)`**. Elimina el fallback "usa la Matriz si no hay branch" — ahora es explícito: si `location_id` es None y el tool lo necesita, pregunta o falla.

### 4.5 Deps de rutas (`app/routes/deps.py`)

Agregar:
```python
async def get_current_location(
    request: Request,
    org: dict = Depends(get_current_org),
) -> dict | None:
    """Resuelve Location por header X-Location-ID, validando que pertenezca a la Org."""
    loc_id = request.headers.get("X-Location-ID")
    if loc_id and loc_id != "all":
        loc = await db.db_get_location_by_id(int(loc_id))
        if not loc or loc["org_id"] != org["id"]:
            raise HTTPException(403, "Location fuera de tu organización")
        return loc
    return None  # "all" o ausente → queries sin filtro de location
```

Todas las rutas operativas que hoy reciben `branch_id` cambian a `location_id` via este dep.

### 4.6 Repos — patrón de cambio

**Antes:**
```python
async def db_get_orders(restaurant_id: int, branch_id: int | None = None):
    async with tenant_connection() as conn:
        if branch_id:
            rows = await conn.fetch("SELECT * FROM orders WHERE restaurant_id=$1 AND branch_id=$2",
                                     restaurant_id, branch_id)
        else:
            rows = await conn.fetch("SELECT * FROM orders WHERE restaurant_id=$1", restaurant_id)
```

**Después:**
```python
async def db_get_orders(location_id: int | None = None):
    # org_id implícito via RLS (app.org_id GUC)
    async with tenant_connection() as conn:
        if location_id:
            rows = await conn.fetch("SELECT * FROM orders WHERE location_id=$1", location_id)
        else:
            rows = await conn.fetch("SELECT * FROM orders")  # todas las sedes de la Org
```

**Signature cleanup:** eliminar `restaurant_id` de signatures de repos Org-scoped (RLS lo hace implícito, como ya pasa hoy con tablas migradas). Mantener `location_id` como param explícito.

### 4.7 Frontend

Cambios en `localStorage`:
- `rb_restaurant` → `rb_org` (shape nuevo)
- Nuevo: `rb_locations` (array), `rb_current_location_id`
- Header `X-Branch-ID` → `X-Location-ID`
- Selector global de sucursal ahora lista `rb_locations` en vez de `db_get_branches`

Archivos afectados: `mesio-utils.js`, `login.html`, `dashboard.html`, todas las páginas con selector de branch. **Zero cambio de UX** — el selector se llama igual, solo cambia el source.

### 4.8 Internal / Superadmin

`app/routes/internal/admin.py` — el CRUD de "restaurantes" se parte en CRUD de Organizations + CRUD de Locations. La UI de `/internal/superadmin` cambia a dos tabs: "Organizaciones" y "Sedes".

Endpoints nuevos:
```
GET    /api/internal/admin/organizations
POST   /api/internal/admin/organizations
PATCH  /api/internal/admin/organizations/{id}
GET    /api/internal/admin/organizations/{id}/locations
POST   /api/internal/admin/organizations/{id}/locations
PATCH  /api/internal/admin/locations/{id}
DELETE /api/internal/admin/locations/{id}
```

---

## 5. Cutover strategy (zero-downtime)

| Día | Acción | Riesgo |
|---|---|---|
| **D-7** | Merge Fase 0. `pg_dump` de prod. Baseline de tests en staging. | Bajo |
| **D-6** | Aplicar Fase 1 (create tables + backfill) en staging. Validar conteos. | Bajo (solo lee de restaurants, escribe nuevas tablas) |
| **D-5** | Aplicar Fase 2 (add columns + backfill) en staging. Correr `run_ai_sim.py`. | Medio (backfill en tablas grandes — medir duración) |
| **D-4** | Aplicar Fase 3 (dual RLS policies) en staging. | Bajo |
| **D-3** | Deploy Wave 1 (código dual) a staging. 24h de tráfico simulado + suite completa. | Alto — detección de bugs de migración |
| **D-2** | En una ventana de bajo tráfico (3am CO): aplicar Fase 1-3 en prod. Deploy Wave 1 a prod. | Alto — pero reversible (ver §6) |
| **D-1** | Monitor: latencia p95, error rate, inbox queue depth, dead letters. Alerta especial para `TenantNotSetError`. | Medio |
| **D+7** | Si métricas OK durante 7 días: aplicar Fase 5 en staging, luego prod. Deploy Wave 2. | Medio |
| **D+21** | Aplicar Fase 7 (drop `restaurants_deprecated`). | Bajo |

**Criterio de go/no-go antes de Wave 2:** cero `TenantNotSetError`, cero filas con `org_id IS NULL` en tablas Location-level (monitoring query corriendo cada hora), cero regresiones en métricas de negocio (orders/día, NPS response rate).

---

## 6. Rollback

**Cada fase es reversible individualmente** mientras estemos antes de Fase 5.

### Rollback Fase 1-3 (schema + dual RLS)
`alembic downgrade 0033` (pre-migration) revierte:
- Drop policies nuevas
- Drop columnas `org_id`, `location_id`
- Drop tablas `organizations`, `locations`, `_migration_restaurant_to_location`

Datos en `restaurants` intactos. Código Wave 1 rollback a commit anterior. **Rollback seguro en <5 min.**

### Rollback después de Fase 5
Si se deployó Wave 2 y hay que revertir, hay que:
1. Restaurar `restaurants` desde `pg_dump`
2. Redeploy código pre-migration
3. Rehacer las órdenes/pedidos creados desde el cutover (ventana de pérdida = tiempo entre Fase 5 y rollback)

Para minimizar este riesgo, **mantener la view `restaurants` funcional durante 2-4 semanas post Fase 5**. El drop real (Fase 7) se hace solo después de confirmar estabilidad.

### Kill-switch de emergencia
Feature flag `USE_ORG_SCOPE` en env. Durante Wave 1, toda lógica nueva verifica:
```python
if os.environ.get("USE_ORG_SCOPE") == "0":
    # fallback al modelo viejo
```
Permite desactivar rollout sin rollback de código. Activa después de D+7.

---

## 7. Tests — matriz de cobertura

### 7.1 Tests obligatorios ANTES del merge de cada fase

| Fase | Tests que deben pasar |
|---|---|
| 1 | `pytest tests/test_org_location_migration.py` (nuevo) + suite completa 766/766 |
| 2 | Tests nuevos de backfill: cada tabla tiene `org_id` no-NULL, consistent con `restaurant_id` |
| 3 | `pytest tests/test_rls_dual_policy.py` — verifica que queries pasan bajo ambos GUC |
| 4 (code) | `run_ai_sim.py` — 20 escenarios multi-turno + suite completa |
| 5 | Verificar que tablas Org-level no tienen `restaurant_id`, que view `restaurants` retorna shape esperado |
| 6 (code) | Suite completa + sim 48h en staging sin regresión |

### 7.2 Tests nuevos a escribir (Sonnet los implementa)

```
tests/test_org_location_migration.py
  - test_every_matriz_has_one_org
  - test_every_sucursal_has_one_location
  - test_mapping_table_covers_all_restaurants
  - test_primary_location_unique_per_org

tests/test_org_rls.py
  - test_org_scope_filters_orders_by_org
  - test_location_param_filters_orders_within_org
  - test_cross_org_leak_impossible

tests/test_webhook_routing_org.py
  - test_bot_number_on_org_resolves_org
  - test_bot_number_on_location_resolves_both
  - test_gps_location_resolution_within_org

tests/test_agent_location_nullable.py
  - test_exploratory_chat_without_location
  - test_delivery_resolves_location_at_address_provided
  - test_pickup_asks_location_if_multiple_sedes
  - test_qr_embeds_location_from_message_one

tests/test_backward_compat_restaurants_view.py
  - test_select_from_restaurants_returns_flat_list
  - test_update_restaurants_raises_error
```

### 7.3 Test obligatorio de NO regresión

`run_ai_sim.py` tiene 20 escenarios. **Todos deben pasar con el mismo output** después de la migración. Si alguno cambia, es bug.

---

## 8. Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Backfill deja filas con `org_id = NULL` | Media | Alto (RLS fail-closed = datos invisibles) | Validación con `RAISE EXCEPTION` dentro de la migration. Query de monitoring post-deploy. |
| Dual GUC causa policies contradictorias | Baja | Alto | Tests `test_rls_dual_policy.py`. Fase 3 corrida en staging 48h antes de prod. |
| `whatsapp_number` con sufijo `_b` no se detecta bien | Media | Medio (routing de sucursal con número propio roto) | Query de inspección manual en Fase 1 validación. Lista explícita de excepciones. |
| Migration de `carts` pierde estado en vuelo durante cutover | Baja | Bajo (carts son efímeros, TTL Redis) | Hacer cutover en ventana 3am CO donde cart activity es <1/min. |
| Bug en `db_get_org_by_phone` deja webhooks en queue | Media | Alto (mensajes sin procesar) | Inbox worker tiene retry con backoff. Dead letter monitor alerta a las 5min. |
| Frontend cached con `rb_restaurant` shape viejo | Alta | Bajo (solo usuarios con sesión vieja) | Incrementar version en service worker + forzar re-login. |

---

## 9. Delegación a Sonnet — bloques de trabajo

Cada bloque es self-contained: Sonnet puede implementarlo sin re-leer esta conversación.

### Bloque S1 — Schema + backfill (Fase 1-2)
**Input:** este plan §2, §3 Fase 1-2
**Deliverable:** `alembic/versions/0034_create_organizations_locations.py`, `alembic/versions/0035_add_org_location_columns.py`, tests `tests/test_org_location_migration.py`
**Criterio de aceptación:** `alembic upgrade head` en DB con datos reales de staging termina sin errores, validación interna pasa, test suite pasa.

### Bloque S2 — Dual RLS (Fase 3)
**Input:** este plan §3 Fase 3 + §4.2
**Deliverable:** `alembic/versions/0036_dual_rls_policies.py`, mods en `app/services/tenant_context.py` y `tenant_db.py`, tests `tests/test_org_rls.py` y `tests/test_rls_dual_policy.py`
**Criterio:** queries con `SET app.restaurant_id` Y queries con `SET app.org_id` retornan mismo resultado. Test empírico reproduce §1 de CLAUDE.md (scope=8 → 4 orders, no_scope → 0, cross-tenant INSERT falla).

### Bloque S3 — Repos + auth + deps (Wave 1 Fase 4)
**Input:** este plan §4.1-4.6
**Deliverable:** repos nuevos (`db_get_org_by_phone`, `db_get_org_locations`, `db_get_primary_location`, `db_resolve_location_by_gps`, `db_get_location_by_id`), actualización de `auth.py` y `deps.py` (con aliases deprecated), actualización de signatures en repos afectados
**Criterio:** login retorna shape nuevo con `org` + `locations`. Todas las rutas existentes siguen funcionando. 766/766 tests pasan.

### Bloque S4 — Agent + inbox_worker + bot logic (Wave 1 Fase 4)
**Input:** este plan §4.3-4.4 + sección "Reglas del Bot" de CLAUDE.md
**Deliverable:** `inbox_worker._handle_meta_whatsapp` resuelve Org en lugar de restaurant, `agent._resolve_location_id` reemplaza `_resolve_branch_id`, conversation state incluye `location_id`, tool calls propagan `location_id`
**Criterio:** `run_ai_sim.py` 20 escenarios pasan. Especialmente importante los escenarios de delivery (location resuelve por GPS) y pickup (asks if multiple).

### Bloque S5 — Frontend (Wave 1 Fase 4)
**Input:** este plan §4.7
**Deliverable:** `mesio-utils.js` actualizado (`rb_org`, `rb_locations`), header `X-Location-ID`, login pages, selector global
**Criterio:** sesión fresca login → dashboard carga → selector muestra locations → pedidos filtran por location seleccionada. Sesión vieja (con `rb_restaurant`) es auto-migrada a nuevo shape en el primer fetch o forzada a re-login.

### Bloque S6 — Drop legacy (Fase 5-6)
**Input:** este plan §3 Fase 5-6
**Deliverable:** `0037_drop_legacy_rls_and_restaurant_id.py`, view `restaurants` retrocompat, cleanup de GUC dual en `tenant_db.py`
**Criterio:** Tests pasan, `SELECT * FROM restaurants` retorna shape retrocompat, `INSERT INTO restaurants` falla con mensaje claro.

### Bloque S7 — Internal admin UI (cualquier momento post Wave 2)
**Input:** este plan §4.8
**Deliverable:** endpoints nuevos, `app/static/html/internal/superadmin.html` actualizado con 2 tabs
**Criterio:** puedo crear un Org, agregarle 3 locations, asignar WhatsApp a una sola, y ver pedidos filtrados por location.

---

## 10. Checklist pre-merge (antes de cualquier commit)

- [ ] `pytest tests/ --ignore=tests/ai_sim -x` → 766/766 pasan
- [ ] `python run_ai_sim.py` → 20/20 escenarios pasan (requiere Postgres + Anthropic reales)
- [ ] `alembic upgrade head && alembic downgrade 0033 && alembic upgrade head` sin errores (idempotencia)
- [ ] Ninguna query de monitoring reporta `org_id IS NULL` en tablas Location-level
- [ ] `grep -rE "\.restaurant_id\b" app/services app/routes` — cero hits (o todos en `# legacy` comentados)
- [ ] `ALERT_WEBHOOK_URL` configurado en staging y prod — alertas funcionan
- [ ] Doc `CLAUDE.md` actualizado con nuevo modelo antes del merge final

---

## 11. Estimación de esfuerzo

| Bloque | Sonnet-hours | Dependencias |
|---|---|---|
| S1 Schema + backfill | 4-6h | — |
| S2 Dual RLS | 2-3h | S1 |
| S3 Repos + auth + deps | 8-12h | S2 |
| S4 Agent + inbox_worker | 6-10h | S3 |
| S5 Frontend | 4-6h | S3 |
| S6 Drop legacy | 2-3h | S4 + S5 en prod 7d |
| S7 Internal admin | 3-4h | S6 |
| **Total implementación** | **29-44h** | |
| **Total calendar (con cutover + observación)** | **3-4 semanas** | |

---

## 12. Apéndice — queries útiles de inspección

```sql
-- Ver mapping actual de restaurants → org/location (después de Fase 1)
SELECT
  r.id AS old_restaurant_id,
  r.name,
  r.parent_restaurant_id,
  m.org_id,
  m.location_id,
  l.is_primary
FROM restaurants r
LEFT JOIN _migration_restaurant_to_location m ON m.old_restaurant_id = r.id
LEFT JOIN locations l ON l.id = m.location_id
ORDER BY m.org_id, l.is_primary DESC;

-- Detectar filas con org_id NULL después de Fase 2 (debe ser 0)
SELECT 'orders' AS tbl, COUNT(*) FROM orders WHERE org_id IS NULL UNION ALL
SELECT 'staff',         COUNT(*) FROM staff  WHERE org_id IS NULL UNION ALL
SELECT 'conversations', COUNT(*) FROM conversations WHERE org_id IS NULL UNION ALL
-- ... 33 tablas

-- Verificar invariante "1 primary por org"
SELECT org_id, COUNT(*) FROM locations WHERE is_primary = true GROUP BY org_id HAVING COUNT(*) <> 1;

-- Latencia de queries post-Fase 3 (comparar pre/post)
SELECT schemaname, tablename, n_tup_ins, n_tup_upd, n_tup_del
FROM pg_stat_user_tables WHERE tablename IN ('orders','staff','conversations');
```

---

**FIN DEL PLAN.**

Próximo paso sugerido: revisar §2.3 (clasificación Org/Location de las 33 tablas) — cualquier reclasificación cambia el backfill. Especialmente revisar las dudosas: `loyalty_ledger` (¿los puntos se ganan Org-wide o por sede?), `weekly_reports` (¿reporte consolidado o por sede?), `payroll_runs` (¿nómina Org o por sede?).
