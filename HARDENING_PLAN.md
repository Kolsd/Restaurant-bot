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

## Fase 2: Monitoring + Worker Separado  ✅ COMPLETADA

### Lo que se hizo

**2.1 Lifespan moderno** (`app/main.py`)
- `@app.on_event("startup")`/`@app.on_event("shutdown")` → `@asynccontextmanager lifespan(app)`
- Globals `_inbox_stop_event`/`_inbox_task` eliminados → variables locales dentro del lifespan
- DeprecationWarnings eliminados

**2.2 Inbox worker como proceso separado**
- `scripts/run_inbox_worker.py` — entrypoint standalone con signal handling (SIGTERM/SIGINT)
- `railway.toml` — start command condicional: `WORKER_MODE=inbox` → worker, default → uvicorn
- `DISABLE_EMBEDDED_WORKER=1` env var para que HTTP no arranque worker interno
- Railway: servicio `inbox-worker` desplegado y operativo. HTTP crash ya no mata webhook processing.

**2.3 Metricas operativas** (`app/routes/health.py`)
- `GET /health/metrics` con auth via `ADMIN_KEY` env var
- Retorna: `db_pool_size`, `db_pool_free`, `db_pool_used`, `inbox_queue_depth`, `inbox_dead_letters`
- Cada metrica en su propio try/except (resiliente a DB down)

### Tests creados
| Archivo | Tests | Que cubre |
|---|---|---|
| `tests/test_lifespan.py` | 3 | Startup OK, worker disabled by env, worker enabled by default |
| `tests/test_health.py` | +5 | Metrics auth (401), wrong key, valid data, pool arithmetic, no ADMIN_KEY |

### Bugs encontrados: 0
(Fase 2 fue refactor de arquitectura, no de logica financiera)

---

## Fase 3: Hardening de Flujos Core  ✅ COMPLETADA

### Lo que se hizo

**3.1 Integration tests end-to-end** (`tests/test_integration_flows.py`)
- 13 tests contra PostgreSQL real (transaction rollback para aislamiento)
- `TestDeliveryOrderFlow` (3 tests): commit + deduct stock, insufficient stock rollback, order sin inventory link
- `TestTableCheckFlow` (3 tests): pay con tip persistido, tip cap 50%, unique constraint en split checks
- `TestTipDistributionFlow` (5 tests): single role, two-role split, staff off-shift → unallocated, non-invoiced exclusion, multi-check accumulation
- `TestShiftConstraints` (2 tests): partial unique index prevents double open shift

**3.2 Rate limiting via Redis** (`app/services/state_store.py`, `app/routes/tables.py`)
- `rate_limit_check(key, max_requests, window_seconds)` en `state_store.py` — Redis INCR+EXPIRE con fallback in-process sliding window
- `POST .../checks/{id}/pay` — 3 req/check/10s (previene doble-pago por doble-click)
- Webhook ya tenia rate limit: 20 msgs/60s por phone via Postgres (`meta_rate_limits`), no se modifico
- `conftest.py` — autouse fixture que limpia `_fb_rate_limits` entre tests

**3.3 Load testing** (`scripts/load_test.py`)
- Script standalone: `DATABASE_URL=... python scripts/load_test.py --concurrency 50 --stock 100`
- Valida que `SELECT FOR UPDATE` + `UPDATE WHERE current_stock >= $1` previene lost updates
- Verifica consistencia: `final_stock == initial_stock - successes`
- Reporta latencia p50/p95/p99 y throughput
- Cleanup automatico de datos de test

### Tests creados
| Archivo | Tests | Que cubre |
|---|---|---|
| `tests/test_integration_flows.py` | 13 | Flujos E2E: delivery, table→pay→tip, payroll, constraints |
| `tests/test_rate_limit.py` | 4 | rate_limit_check fallback, pay_check 429 |

### Bugs encontrados: 0
(Los 6 bugs criticos se encontraron en Fase 1. Fases 2-3 validaron que la arquitectura es correcta)

---

## Pre-existentes no relacionados (para referencia)
- `tests/test_core.py` tiene 2 failures pre-existentes (`test_login_user_not_found`, `test_login_rate_limit`) — no son del hardening, son del modulo de auth legacy.
- `billing.py` sigue con SQL directo — es capa fiscal/DIAN, diferido intencionalmente.
- Migracion `0012_b2b_sales_system.py` crea tablas huerfanas — cleanup pendiente.
