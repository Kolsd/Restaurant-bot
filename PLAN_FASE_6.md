# PLAN FASE 6 — Staff, Clock-in/Clock-out y Tip Splitting

> Documento de planificación arquitectónica. Sin código. Para implementar en la siguiente sesión.

---

## 1. Contexto y Objetivos

FASE 6 agrega tres capacidades interconectadas:

1. **Registro de personal (Staff)** — catálogo de empleados por restaurante con rol y estado activo.
2. **Reloj Checador (staff_shifts)** — entrada/salida con timestamp preciso; un empleado solo puede tener un turno abierto a la vez.
3. **Propina Voluntaria (Tip) y su distribución** — almacenamiento de la propina del 10% voluntario en `table_checks`, cálculo del pool al cierre de turno y reparto porcentual configurable por rol.

---

## 2. Modelo de Datos

### 2.1 Tabla `staff`

```sql
CREATE TABLE IF NOT EXISTS staff (
    id              SERIAL PRIMARY KEY,
    restaurant_id   INTEGER NOT NULL,           -- tenant FK (no FK formal para evitar dependencia circular)
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,              -- 'mesero' | 'cocina' | 'bar' | 'caja' | 'admin'
    pin             TEXT,                       -- PIN de 4 dígitos hasheado con bcrypt (opcional, para clock-in sin sesión web)
    hourly_rate     NUMERIC(10,2) DEFAULT 0,    -- solo informativo, para reportes de nómina futura
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (restaurant_id, name)                -- no duplicados por nombre en el mismo restaurante
);

CREATE INDEX IF NOT EXISTS idx_staff_restaurant ON staff(restaurant_id, active);
```

**Notas de diseño:**
- `role` es texto libre controlado por el frontend (no un ENUM) para no bloquear migraciones futuras.
- `pin` es opcional: restaurantes pequeños pueden no usarlo. Si está presente, se valida con `bcrypt.checkpw` igual que las contraseñas de usuario.
- `hourly_rate` se deja en 0 por defecto. No se usa en FASE 6, pero evita una migración posterior cuando se implemente nómina.

---

### 2.2 Tabla `staff_shifts`

```sql
CREATE TABLE IF NOT EXISTS staff_shifts (
    id              SERIAL PRIMARY KEY,
    staff_id        INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    restaurant_id   INTEGER NOT NULL,           -- desnormalizado para queries rápidos sin JOIN
    clock_in        TIMESTAMP NOT NULL DEFAULT NOW(),
    clock_out       TIMESTAMP,                  -- NULL = turno todavía abierto
    duration_min    INTEGER,                    -- calculado al hacer clock_out: (clock_out - clock_in) en minutos
    notes           TEXT,                       -- ej: "turno partido", "cubriendo a Pedro"
    created_at      TIMESTAMP DEFAULT NOW(),

    CHECK (clock_out IS NULL OR clock_out > clock_in)
);

-- GARANTIA CRITICA: un empleado no puede tener dos turnos abiertos simultáneamente
-- Este índice único parcial es la restricción a nivel de DB (no solo en app)
CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_shifts_one_open
    ON staff_shifts(staff_id)
    WHERE clock_out IS NULL;

CREATE INDEX IF NOT EXISTS idx_staff_shifts_restaurant_date
    ON staff_shifts(restaurant_id, clock_in DESC);

CREATE INDEX IF NOT EXISTS idx_staff_shifts_staff_date
    ON staff_shifts(staff_id, clock_in DESC);
```

**Notas de diseño:**
- `duration_min` es calculado y persistido en el momento del `clock_out` (no es columna generada, para evitar incompatibilidades si asyncpg no soporta columnas generadas en asyncpg record).
- El índice único parcial `WHERE clock_out IS NULL` es la protección definitiva contra turnos dobles. Si la app intenta insertar un segundo turno abierto para el mismo empleado, PostgreSQL lanza `UniqueViolationError` que se captura y convierte en HTTP 400.
- El `clock_out` se valida `> clock_in` con un `CHECK` constraint para evitar errores de UI.

---

### 2.3 Columna `tip_amount` en `table_checks`

La tabla `table_checks` (creada en FASE 5) necesita una columna adicional. Se agrega con `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` en `db_init_table_checks()` para que sea idempotente (no rompe instancias existentes):

```sql
ALTER TABLE table_checks
    ADD COLUMN IF NOT EXISTS tip_amount NUMERIC(10,2) NOT NULL DEFAULT 0;
```

**Notas de diseño:**
- `tip_amount` se almacena **fuera** del `total` fiscal. El `total` en `table_checks` representa el valor del consumo sujeto a IVA/INC. La propina en Colombia **no es base gravable** (no paga IVA ni INC), por lo tanto la factura electrónica DIAN se emite por `total` (sin propina). La propina se informa al cliente en el ticket impreso como ítem separado.
- El pago final que el cliente entrega cubre `total + tip_amount`. El campo `payments` JSONB ya registra el monto real recibido (que puede incluir propina + cambio). Separar `tip_amount` permite calcular el pool de propinas con un `SUM` simple.

---

### 2.4 Tabla `tip_distributions`

Registra de forma inmutable cada vez que el administrador ejecuta el reparto de propinas al final de un turno:

```sql
CREATE TABLE IF NOT EXISTS tip_distributions (
    id              SERIAL PRIMARY KEY,
    restaurant_id   INTEGER NOT NULL,
    period_start    TIMESTAMP NOT NULL,
    period_end      TIMESTAMP NOT NULL,
    total_tips      NUMERIC(10,2) NOT NULL,     -- SUM(tip_amount) en el período
    distribution    JSONB NOT NULL,
    -- Snapshot del reparto:
    -- [{"staff_id": 1, "name": "Ana", "role": "mesero", "amount": 15000},
    --  {"staff_id": 2, "name": "Carlos", "role": "cocina", "amount": 9000}, ...]
    pct_config      JSONB NOT NULL,
    -- Config usada para el cálculo:
    -- {"mesero": 50, "cocina": 30, "bar": 20}
    created_by      TEXT NOT NULL,              -- username del admin que ejecutó el reparto
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tip_dist_restaurant ON tip_distributions(restaurant_id, created_at DESC);
```

**Notas de diseño:**
- La tabla es append-only (no se modifica). Cada ejecución crea un registro nuevo. Esto protege la auditoría.
- `pct_config` almacena la configuración usada en el momento del cálculo, no la actual. Si el dueño cambia los porcentajes mañana, los repartos históricos conservan los porcentajes que se usaron.
- `distribution` es un snapshot completo, no referencias a IDs. Si un empleado es eliminado, el historial de repartos permanece legible.

---

### 2.5 Configuración de Porcentajes (sin tabla nueva)

Los porcentajes de distribución de propinas se guardan en el JSONB `features` del restaurante (tabla `restaurants` existente):

```json
{
  "tip_distribution": {
    "mesero": 50,
    "cocina": 30,
    "bar": 20
  }
}
```

Si `tip_distribution` no existe en `features`, el sistema usa los defaults `{mesero: 50, cocina: 30, bar: 20}`. Esta configuración se edita desde el dashboard de settings (sin nueva tabla).

---

## 3. Lógica de Negocio — Propina y Distribución

### 3.1 Flujo de Propina en Caja (FASE 5 + FASE 6)

```
Cajero abre pay modal para un check (FASE 5)
    → Check total: $90,000
    → "Propina voluntaria sugerida (10%): $9,000"
    → Campo editable: tip_amount = [9000]  ← el cliente puede cambiar a 0 o a otro valor
    → payments = [{"method": "efectivo", "amount": 99000}]
    → Backend: valida Σ payments >= (check.total + tip_amount)
    → Factura DIAN: emitida por $90,000 (check.total)
    → Ticket impreso: muestra $90,000 + Propina: $9,000 + Total pagado: $99,000
    → DB: UPDATE table_checks SET tip_amount = 9000, payments = [...], change_amount = 0
```

**Regla crítica:** La factura electrónica DIAN siempre se emite por `check.total` (sin propina). La propina no es ingreso gravable del restaurante; es una transferencia voluntaria del cliente hacia el personal.

### 3.2 Cálculo del Pool de Propinas

```
GET /api/staff/tips/pool?from=2026-03-25T08:00:00&to=2026-03-25T23:59:59

SQL:
    SELECT SUM(tip_amount) AS total_pool
    FROM table_checks
    WHERE restaurant_id = $1
      AND paid_at BETWEEN $2 AND $3
      AND tip_amount > 0
      AND status = 'invoiced'
```

Resultado: `{"total_pool": 87000, "checks_count": 12, "period": {...}}`

### 3.3 Algoritmo de Distribución al Final del Turno

```
Input:
  - total_pool = 87,000
  - pct_config = {"mesero": 50, "cocina": 30, "bar": 20}
  - period = turno del día (clock_in BETWEEN from AND to)

Paso 1 — Calcular el monto por rol:
  mesero_pool = 87,000 × 0.50 = 43,500
  cocina_pool = 87,000 × 0.30 = 26,100
  bar_pool    = 87,000 × 0.20 = 17,400

Paso 2 — Identificar staff activo durante el turno:
  Query: SELECT DISTINCT s.id, s.name, s.role
         FROM staff s
         JOIN staff_shifts sh ON sh.staff_id = s.id
         WHERE s.restaurant_id = $1
           AND s.active = TRUE
           AND sh.clock_in < $period_end
           AND (sh.clock_out IS NULL OR sh.clock_out > $period_start)
           AND s.role IN ('mesero', 'cocina', 'bar')

  Resultado:
    Meseros: [Ana, Luis]  → 2 personas
    Cocina:  [Carlos]     → 1 persona
    Bar:     [María]      → 1 persona

Paso 3 — División igualitaria dentro de cada rol:
  Ana   (mesero): 43,500 / 2 = 21,750
  Luis  (mesero): 43,500 / 2 = 21,750
  Carlos (cocina): 26,100 / 1 = 26,100
  María  (bar):    17,400 / 1 = 17,400

  Nota de redondeo: si el cociente no es entero, el excedente de centavos
  se asigna al primer empleado de la lista (alfabético por nombre).

Paso 4 — Persistir en tip_distributions (registro inmutable).

Resultado retornado al frontend:
  {
    "total_pool": 87000,
    "distribution": [
      {"name": "Ana",    "role": "mesero", "amount": 21750},
      {"name": "Luis",   "role": "mesero", "amount": 21750},
      {"name": "Carlos", "role": "cocina", "amount": 26100},
      {"name": "María",  "role": "bar",    "amount": 17400}
    ],
    "pct_config": {"mesero": 50, "cocina": 30, "bar": 20}
  }
```

**Casos especiales:**
- Si no hay meseros activos en el turno: el `mesero_pool` se redistribuye proporcionalmente entre los roles restantes (no se pierde).
- Si el `total_pool` es 0: el endpoint retorna éxito con amounts todos en 0 (no lanza error).
- Si un rol tiene 0% asignado en la config: ese rol no aparece en el distribution array.

---

## 4. Plan de Commits (4 atómicos)

### Commit 1 — DB layer (`database.py`, `main.py`)

**Funciones nuevas:**
- `db_init_staff()` — `CREATE TABLE staff` + index
- `db_init_staff_shifts()` — `CREATE TABLE staff_shifts` + índice único parcial
- `db_init_tip_distributions()` — `CREATE TABLE tip_distributions`
- `db_add_tip_amount_to_checks()` — `ALTER TABLE table_checks ADD COLUMN IF NOT EXISTS tip_amount`
- `db_create_staff(restaurant_id, name, role, pin, hourly_rate)`
- `db_get_staff(restaurant_id, include_inactive=False)`
- `db_update_staff(staff_id, fields)`
- `db_clock_in(staff_id, restaurant_id)` — INSERT con protección del índice único parcial
- `db_clock_out(staff_id)` — UPDATE clock_out + calcula duration_min
- `db_get_open_shifts(restaurant_id)` — todos los turnos sin clock_out
- `db_get_shift_history(staff_id, from_dt, to_dt)`
- `db_get_tip_pool(restaurant_id, from_dt, to_dt)` — SUM + count
- `db_get_active_staff_for_period(restaurant_id, from_dt, to_dt)` — staff con turno en el rango
- `db_save_tip_distribution(restaurant_id, period_start, period_end, total_pool, distribution, pct_config, created_by)`
- `db_get_tip_distributions(restaurant_id, limit=20)`

**`main.py`:** agregar las 4 funciones de init al startup, en orden (después de `db_init_table_checks`).

---

### Commit 2 — Routes (`app/routes/staff.py` nuevo + registro en `main.py`)

**Endpoints Staff:**
```
POST   /api/staff                        — crear empleado
GET    /api/staff                        — listar (query param: ?include_inactive=true)
PUT    /api/staff/{id}                   — actualizar (nombre, rol, pin, hourly_rate, active)
DELETE /api/staff/{id}                   — desactivar (soft delete: active=False)
```

**Endpoints Clock:**
```
POST   /api/staff/{id}/clock-in          — body: {"pin": "1234"} (opcional)
POST   /api/staff/{id}/clock-out         — body: {"notes": "..."} (opcional)
GET    /api/staff/shifts/active          — todos los turnos abiertos ahora
GET    /api/staff/{id}/shifts            — historial con query params ?from=&to=
```

**Endpoints Tips:**
```
GET    /api/staff/tips/pool              — query: ?from=&to= → {total_pool, checks_count}
POST   /api/staff/tips/distribute        — body: {from, to, pct_config_override?}
                                            → calcula, persiste, retorna distribution
GET    /api/staff/tips/distributions     — historial de repartos (limit 20)
```

**Modificación a `tables.py`:** El endpoint `POST /checks/{id}/pay` recibirá `tip_amount` en el body (default 0) y lo persistirá junto al pago.

---

### Commit 3 — Frontend (`app/static/staff.html` nuevo)

**Estructura de la página (misma estructura visual que `caja.html`):**

**Panel 1 — Empleados y Reloj:**
- Tabla con columnas: Nombre | Rol | Estado (En turno / Fuera) | Hora entrada | Acciones
- Botón "Registrar Entrada" / "Registrar Salida" por fila (verde/rojo según estado)
- Modal de PIN (si el empleado tiene PIN configurado): 4 dígitos con keypad numérico
- Botón "Nuevo Empleado" → modal de creación (nombre, rol, PIN opcional, tarifa/hora opcional)

**Panel 2 — Propinas del Turno:**
- Date range picker: "Desde" y "Hasta" (defaultea a hoy 06:00 — ahora)
- Botón "Calcular Pool" → muestra: "Pool total: $87,000 | 12 checks | Promedio: $7,250"
- Desglose por rol con los porcentajes configurados
- Tabla de distribución por empleado (preview)
- Botón "Distribuir y Registrar" → confirmación → POST /distribute → snapshot guardado
- Historial de distribuciones anteriores (últimas 10, colapsable)

**Caja.html (modificación mínima):**
- En el `pay-modal` de FASE 5: agregar campo "Propina (opcional)" con sugerencia 10% calculada en vivo
- El monto de propina se envía como `tip_amount` al endpoint `/pay`

**Roles.js:** agregar enlace a `staff.html` en el nav de roles para usuarios con rol `owner` o `admin`.

---

### Commit 4 — Tests (`tests/test_staff.py`)

Tests a cubrir:

**Clock-in / Clock-out:**
- `test_clock_in_crea_turno_abierto`
- `test_clock_in_duplicado_lanza_error_409` — índice único parcial protege
- `test_clock_out_calcula_duration_min_correctamente`
- `test_clock_out_sin_turno_abierto_lanza_404`

**Propina:**
- `test_tip_pool_suma_checks_del_periodo`
- `test_tip_pool_excluye_checks_sin_tip`
- `test_tip_pool_excluye_checks_no_invoiced`

**Distribución:**
- `test_distribucion_calcula_monto_por_rol_y_persona`
- `test_distribucion_redondeo_centavos`
- `test_distribucion_sin_staff_de_rol_redistribuye`
- `test_distribucion_persiste_snapshot_inmutable`

**Endpoints HTTP:**
- `test_clock_in_endpoint_ok`
- `test_clock_in_endpoint_pin_incorrecto_401`
- `test_distribute_endpoint_ok`
- `test_distribute_endpoint_sin_pool_retorna_cero`

---

## 5. Decisiones de Diseño Pendientes (para confirmar al inicio de la sesión)

1. **PIN de empleados**: el clock-in desde la UI de staff.html ¿requiere siempre PIN, o solo si el empleado tiene uno configurado? Recomendación: el PIN es opcional por empleado; si no tiene, cualquier admin puede registrar su entrada.

2. **Múltiples turnos en un día**: si un empleado hace turno partido (entra, sale, vuelve a entrar), ¿se crean 2 filas en `staff_shifts` o se maneja como un único turno con pausa? Recomendación: 2 filas independientes (más simple, el índice único parcial lo permite perfectamente porque el primer turno ya tiene `clock_out != NULL`).

3. **Propina en "Cobrar Todo"**: ¿La sugerencia del 10% se calcula sobre el `total` del check (precio con IVA incluido) o sobre el `subtotal` (base gravable)? En Colombia la costumbre es calcular sobre el **total con IVA**. El documento asume esta opción.

4. **Distribución de propinas a empleados sin turno activo**: ¿Se les incluye en el reparto si trabajaron durante el período aunque ya hayan registrado salida? Recomendación: **sí** — la query usa `clock_in < period_end AND (clock_out IS NULL OR clock_out > period_start)`, lo que incluye a quienes trabajaron dentro del rango aunque ya salieron.

---

## 6. Resumen del Esquema Completo

```
staff             (id, restaurant_id, name, role, pin, hourly_rate, active)
staff_shifts      (id, staff_id, restaurant_id, clock_in, clock_out, duration_min, notes)
  + UNIQUE INDEX  (staff_id) WHERE clock_out IS NULL   ← garantía 1 turno abierto
tip_distributions (id, restaurant_id, period_start, period_end, total_tips, distribution JSONB,
                   pct_config JSONB, created_by)
table_checks      (... columnas FASE 5 ..., tip_amount NUMERIC DEFAULT 0)   ← ALTER ADD COLUMN
restaurants.features.tip_distribution = {"mesero": 50, "cocina": 30, "bar": 20}  ← sin tabla nueva
```

Total: 3 tablas nuevas + 1 columna nueva + 1 campo JSONB en tabla existente.
