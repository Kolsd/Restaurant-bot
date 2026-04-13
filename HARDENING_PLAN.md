# Mesio Hardening Roadmap

## Context

Mesio es un SaaS multi-tenant para restaurantes que maneja dinero real (nomina, propinas, pagos, inventario). Despues del refactor v10.1 la arquitectura esta limpia. Prioridad: **correctitud financiera > disponibilidad > cleanup arquitectonico**.

---

## Fase 1: Test Suite Financiero + Health  ✅ COMPLETADA

### Lo que se hizo
- `pyproject.toml` con `asyncio_mode = "auto"`
- `pytest>=8.0`, `pytest-asyncio>=0.24` en `requirements.txt`
- `tests/conftest.py` con fixtures `db_pool` y `db_conn` (real DB + transaction rollback)

### Tests creados (389 tests verdes contra Railway PostgreSQL real)
| Archivo | Tests | Que cubre |
|---|---|---|
| `tests/test_money.py` | 54 | Todos los helpers de `money.py`: `currency_exponent`, `to_decimal`, `quantize_money`, `money_sum`, `money_mul` |
| `tests/test_attendance_deduction.py` | 8 | Tolerancia 5 min, aritmetica de deducciones, timezone parametrizable |
| `tests/test_tips.py` | 9 | Distribucion por attendance, redondeo, roles sin staff, filtro por branch |
| `tests/test_payroll.py` | 8 | Happy path, staff sin turnos, deducciones malformadas, scope por branch, multiples staff |
| `tests/test_order_transaction.py` | 5 | ACID commit, InsufficientStockError, OrderCommitError, coercion Decimal |
| `tests/test_split_checks.py` | +3 | Regression: tip cap incluye service charge |
| `tests/test_health.py` | 7 | Unit (mock pool) + integration (real DB) |

### Bugs de produccion encontrados y corregidos
1. **Timestamp naive/aware en propinas** (`staff_repo.py:658-659`) — `table_checks.paid_at` es `timestamp` (naive) pero `staff_shifts` es `timestamptz`. PostgreSQL usaba timezone de sesion (America/Bogota = UTC-5) para comparar, causando que empleados desaparecieran del calculo de propinas. Fix: `paid_at.replace(tzinfo=timezone.utc)` al leer de DB.

2. **asyncpg string binding** (`staff_repo.py`) — 6 funciones pasaban strings de fecha directo a asyncpg con `::timestamptz`. asyncpg requiere objetos `datetime`. Fix: helper `_ensure_datetime()` en 6 funciones: `db_calculate_tips_by_attendance`, `db_calculate_tip_pool`, `db_save_tip_distribution`, `db_get_timecard`, `db_get_attendance_report`, `db_edit_shift`.

3. **Deducciones malformadas crash** (`staff_repo.py:1365-1371`) — Si `staff.deductions` era un JSON no-dict (ej: `""`), `db_calculate_payroll` crasheaba con `AttributeError`. Fix: guard `if not isinstance(deductions_cfg, dict): deductions_cfg = {}`.

4. **Tip cap no incluia service charge** (`tables.py:932`) — El cap del 50% solo usaba `check.total`, no `total + service_charge`. Fix: `tip_cap_base = to_decimal(check["total"]) + to_decimal(body.service_charge)`.

5. **Timezone hardcoded** (`staff_repo.py:69`) — `_record_attendance_deduction` usaba `America/Bogota` hardcoded. Fix: parametro `restaurant_tz` con default.

6. **branch_id no se propagaba** (`staff_repo.py:~1299`) — `db_calculate_payroll` no pasaba `branch_id` a `db_calculate_tips_by_attendance`. Fix: agregar kwarg.

### Health endpoint creado
- `GET /health` → `200 {"status":"ok","db":"ok"}` | `503 {"status":"degraded","db":"error"}`
- Sin auth (Railway health probe)
- `app/routes/health.py` wired en `app/main.py`

### Notas tecnicas importantes
- **Tests usan DB real, no mocks** — Cada test corre dentro de una transaccion que se hace rollback al final. Pool shim redirige `pool.acquire()` a la conexion de test.
- **`_dt_naive()` helper** — Para columnas `timestamp without time zone` (como `table_checks.paid_at`), asyncpg rechaza datetimes con timezone. Los tests usan `_dt_naive()` que stripea tzinfo.
- **Propinas se calculan por clock-in/clock-out real** — Si el empleado esta programado 6-14 pero hace clock-in a las 7, solo recibe propinas de checks pagados a partir de las 7.

---

## Fase 2: Monitoring + Worker Separado (Siguiente)

### 2.1 Lifespan moderno
- `app/main.py`: `@app.on_event` → `@asynccontextmanager lifespan`
- Eliminar globals `_inbox_stop_event`/`_inbox_task`
- Esto tambien elimina los DeprecationWarnings que salen en los tests

### 2.2 Inbox worker como proceso separado
- Crear `scripts/run_inbox_worker.py` (entrypoint standalone)
- Railway: segundo servicio `worker` con `startCommand` propio
- Remover `create_task(run_worker(...))` del lifespan HTTP
- **Beneficio: HTTP crash ≠ webhook processing muere**

### 2.3 Metricas operativas
- `GET /health/metrics` (auth required):
  ```json
  {"inbox_queue_depth": 12, "dead_letters": 3, "pool_size": 20, "pool_idle": 18}
  ```

### Archivos a modificar
| Archivo | Cambio |
|---|---|
| `app/main.py` | Reemplazar `on_event` con lifespan context manager |
| `app/services/inbox_worker.py` | Extraer loop principal a funcion reutilizable |
| `scripts/run_inbox_worker.py` | NUEVO — entrypoint standalone para worker |
| `app/routes/health.py` | Agregar `/health/metrics` endpoint |

---

## Fase 3: Hardening de Flujos Core

### 3.1 Integration tests end-to-end
- `tests/integration/` con fixtures dedicadas
- Flujos: delivery order completo, table session → pay → tip, payroll end-to-end
- Estos son flujos multi-paso que cruzan repos (no unitarios)

### 3.2 Rate limiting via Redis
- `POST /webhook` — 1 req/phone/segundo (prevenir spam)
- `POST .../checks/{id}/pay` — 3 req/check/10s (prevenir doble-pago)
- Implementar como middleware o decorador usando `state_store` (Redis con fallback)

### 3.3 Load testing
- Script `scripts/load_test.py` — 50 ordenes concurrentes
- Validar que `FOR UPDATE` en inventory funciona bajo carga
- Medir latencia de `commit_order_transaction` bajo contention

---

## Pre-existentes no relacionados (para referencia)
- `tests/test_core.py` tiene 2 failures pre-existentes (`test_login_user_not_found`, `test_login_rate_limit`) — no son del hardening, son del modulo de auth legacy.
- `billing.py` sigue con SQL directo — es capa fiscal/DIAN, diferido intencionalmente.
- Migracion `0012_b2b_sales_system.py` crea tablas huerfanas — cleanup pendiente.
