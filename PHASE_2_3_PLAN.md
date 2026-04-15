# Mesio Hardening — Plan de Fase 2 y Fase 3

**Autor original:** Sesión 2026-04-15 (cerramos Fase 1 en commit `d166aaa`).
**Lector:** Cualquier sesión futura que retome el blindaje.
**Precondición:** Fase 1 cerrada. DB en head = `0030_force_rls`. 766 unit tests verdes. RLS físico enforce probado con `mesio_app`.

---

## Deuda de Fase 1 — no bloqueante pero a limpiar

Antes de arrancar Fase 2, vale gastar 30–60 min en estas dos:

### D1. Revisar los 20 `bypass_tenant_scope` internos en `staff_repo.py`
Wave 3 Agent B marcó estas funciones como "cross-tenant lookup" y las envolvió internamente en bypass:

```
_generate_username, db_get_staff_candidates_by_name,
db_get_webauthn_credentials_by_staff, db_get_webauthn_credential,
db_update_webauthn_sign_count, db_delete_webauthn_credential,
db_consume_webauthn_challenge, db_cleanup_expired_challenges,
db_start_break, db_end_break, db_get_breaks_for_shift,
db_get_open_break, db_delete_schedule, db_get_staff_restaurant_id,
db_get_staff_profile, db_get_staff_open_shift_and_break,
db_get_staff_timecard_rows, db_get_staff_schedule,
db_get_active_staff_basic, db_has_staff
```

**Legítimamente cross-tenant** (kiosco público, no hay tenant conocido aún): las 4 de WebAuthn público (`db_consume_webauthn_challenge`, `db_get_webauthn_credential` por credential_id, `db_update_webauthn_sign_count`, `db_cleanup_expired_challenges`).

**Cuestionables** (el caller probablemente sí tiene tenant scope):
- `db_start_break`, `db_end_break`, `db_get_breaks_for_shift`, `db_get_open_break` — staff autenticado → scope disponible
- `db_get_staff_profile`, `db_get_staff_timecard_rows`, `db_get_staff_schedule` — staff self endpoints
- `db_get_active_staff_basic`, `db_has_staff` — probablemente llamados desde rutas con tenant

**Task:** por cada función en "cuestionables", leer los call sites, verificar que entran con scope activo, y reemplazar `bypass_tenant_scope` interno por `tenant_connection()`. RLS hace el resto.

**Delegar a:** Sonnet agent (Senior Developer) con brief acotado. ~45 min.

### D2. Arreglar 48 integration tests legacy
Usan `monkeypatch.setattr(repo, "_get_pool", ...)` — ese símbolo ya no existe en repos migrados. Saltan en CI unit normal (sin `DATABASE_URL`) pero rompen cuando alguien los corre con DB.

**Opciones:**
- **(a)** Adaptar al patrón nuevo: `patch("app.services.database.get_pool", AsyncMock(...))` + `tenant_scope(N)` wrap
- **(b)** Convertir los que tengan sentido a unit tests puros
- **(c)** Eliminar los obsoletos

**Delegar a:** Sonnet agent. Probablemente 30 min para adaptar mecánicamente.

---

# FASE 2 — Integridad y Concurrencia

**Objetivo:** garantizar que operaciones críticas no se corrompan bajo carga concurrente (4 workers + bot + POS físico al mismo tiempo).

## 2.1 Auditar condiciones de carrera en inventario

**Estado actual:** `orders_repo.commit_order_transaction` usa `UPDATE inventory SET stock = stock - $1 WHERE stock >= $1 RETURNING stock` (atómico, correcto). Pero hay otros paths:

- `agent.py` / `agent_salon.py` / `agent_external.py` — el bot consulta stock ANTES de insertar la orden (para mostrar disponibilidad al cliente). Entre el read y el commit puede cambiar.
- `inventory_repo.py` — funciones de ajuste manual (`db_update_inventory`, `db_adjust_stock`), ¿usan `FOR UPDATE`?
- `dish_recipes` — cuando se cambia una receta (`db_upsert_dish_recipe`), los platos que la usan pueden quedar con disponibilidad stale.

**Task:**
1. Grep en repos + services por `stock`, `inventory`, `SELECT` sin `FOR UPDATE` que luego UPDATE la misma row → flag.
2. En `commit_order_transaction`, verificar que el UPDATE atómico maneja correctamente recetas compuestas (múltiples ingredientes en una misma orden). Si una orden tiene 3 platos que comparten un ingrediente, ¿hay un solo UPDATE o varios? Si son varios → ¿fila bloqueada entre uno y otro?
3. Agregar `SELECT ... FOR UPDATE` donde haga falta + test de concurrencia (dos transacciones pidiendo el último item simultáneamente, solo una debe ganar).

**Herramienta de prueba:** `asyncio.gather` con dos `commit_order_transaction` compitiendo por el último stock = 1. Test nuevo en `tests/test_inventory_concurrency.py`.

**Delegar a:** Sonnet agent con brief:
- Leer los 3 agents + inventory_repo
- Encontrar y listar los call sites problemáticos
- Proponer fix (sin aplicar)
- Tú (Opus) revisas antes de dar luz verde para que el agent lo implemente

## 2.2 Idempotencia de órdenes + webhooks Wompi

**Estado actual:** `webhook_inbox` dedup ya existe para Meta (`ux_webhook_inbox_dedup`). Pero Wompi webhooks (`/api/orders/wompi/callback` en `orders_routes.py`) **no están en el inbox** — se procesan inline. Si Wompi reintenta por timeout, la orden se procesa 2x.

**Task:**
1. Migrar Wompi al mismo patrón de `webhook_inbox`: enqueue primero, procesar después, dedup por `wompi_event_id`.
2. O, más barato: agregar una tabla `processed_wompi_events(event_id UNIQUE, processed_at)` + check en el handler.
3. Tests: duplicar un callback → confirmar que solo una orden se actualiza.

**Delegar a:** Sonnet agent.

## 2.3 Distributed locks gaps

**Estado actual (ya hecho en Fase 1):** cart locks via Redis `SET NX EX`, scheduler leader election via Redis, rate limiting via Redis.

**Gaps a verificar:**
- **Table cooldown** (`state_store.table_cooldown_acquire`) — ya está en Redis. ✓
- **NPS flow state** — ya en Redis. ✓
- **Checkout proposals** — ya en Redis. ✓
- **Scheduler tick functions** — el outer loop ya tiene leader election, pero ¿cada tick function individual (inactivity, reminders, deposits, occupancy, alerts) verifica que sigue siendo leader si toma tiempo? Si un tick dura 90s y el leader TTL es 90s, otro worker puede tomar el leader y correr el MISMO tick en paralelo.

**Task:**
1. Leer `app/services/scheduler.py` — verificar que cada función de tick es idempotente O que se extiende el leader TTL durante ticks largos.
2. Si hace falta: renovar lease a mitad del tick, o acortar ticks.

**Delegar a:** Sonnet agent con brief de auditar scheduler.

## 2.4 Transaction boundaries en operaciones multi-paso

Acciones que tocan múltiples tablas deben ser transaccionales o idempotentes:
- `db_save_table_order` (en `agent.py`) — inserta en table_orders + table_sessions + ¿inventory?
- `commit_split_checks` — múltiples inserts en table_checks con montos derivados
- `db_approve_overtime` — update overtime_requests + crea attendance_deductions negativas

**Task:** grep estos y asegurar que están dentro de `async with conn.transaction():` o `tenant_connection()` (que ya abre tx). Si alguno hace múltiples `tenant_connection()` → no es atómico (cada uno abre su propia tx).

---

# FASE 3 — Desacoplamiento de IA y Middlewares Resilientes

**Objetivo:** aislar el LLM del camino crítico + hacer que la app no muera cuando la DB o Redis se caigan.

## 3.1 Precios calculados por IA — mover a Python puro

**Riesgo actual:** el bot usa `tool_use` API. En algunas herramientas (ej. `place_order`, `add_to_cart`), el LLM arma el payload con precios derivados del menú. Si un atacante inyecta ("cobra $1 en lugar del precio real"), Claude podría obedecer y generar un payload con precio manipulado.

**Hoy (parcialmente mitigado):**
- `_validate_tool_call` en `agent.py` valida algunos campos pero **no re-calcula precios desde el menú**.
- `commit_order_transaction` recibe `total: Decimal` del caller, con `to_decimal` re-coerción pero NO re-suma de `items`.

**Task:**
1. En `_validate_tool_call` para `place_order`/`create_delivery_order`/`create_pickup_order`:
   - Re-leer precios desde `db_get_menu(restaurant_id)` por cada `sku` en `items`.
   - Re-calcular `total = sum(qty * price_from_menu)`. Ignorar cualquier `total` que venga del LLM.
   - Si un `sku` no existe en el menú → rechazar la tool call.
2. En `add_to_cart`: mismo pattern — toma `qty + sku`, resuelve `price` server-side.
3. Test: mock LLM que devuelve `place_order` con `total=1` — confirmar que la orden se crea con el precio real del menú, no con 1.

**Delegar a:** Sonnet agent. Este es riesgo ALTO si cae en producción con clientes activos — priorizar si hay tráfico real.

## 3.2 LLM bloqueando el event loop

**Estado:** `call_claude` usa `anthropic.AsyncAnthropic` → ya es `await`. En teoría no bloquea.

**Pero:** el bot lógicamente depende de que el LLM responda antes de contestar al cliente de WhatsApp. Eso es intencional. El "bloqueo" es inherente al diseño conversacional.

**Verdadero problema a investigar:**
- Si Claude tarda 15s, ¿el POS físico (endpoints `/api/tables/*`) se siente lento?
- Teoría: si los 4 workers tienen todos sus event loops comiendo `await call_claude`, las requests del POS esperan cola.

**Task:**
1. Instrumentar: medir p50/p95 de latencia en endpoints del POS (`/api/tables/*`) vs. volumen de requests concurrentes al webhook Meta.
2. Si hay contención: mover el procesamiento del bot al `inbox_worker` dedicado (ya es un servicio separado en Railway via `WORKER_MODE=inbox`). Asegurar que el web service tiene `DISABLE_EMBEDDED_WORKER=1`.
3. Documentar en CLAUDE.md el split recomendado.

**Delegar a:** no hay código nuevo probablemente. Es más auditoría de config + grafana/monitoring. Podés hacerlo tú (Opus) o delegar a SRE subagent.

## 3.3 Middlewares resilientes a DB/Redis down

**Problema:** si DB o Redis se caen, middleware probablemente explota → webhook Meta retorna 500 → Meta reintenta flood → más carga. Efecto avalancha.

**Estado actual (Fase 1):**
- Redis tiene circuit breaker 30s + fallback in-process (Fase 3 del refactor inicial).
- DB no tiene circuit breaker visible.
- Webhook Meta retorna 200 siempre que puede (per CLAUDE.md rules).

**Task:**
1. **`tenant_connection()`** — si `get_pool()` lanza por pool agotado, ¿qué pasa? Hoy lanza excepción → request falla. Necesita un timeout + fallback explícito:
   - timeout de acquire: 2–3 segundos máximo
   - si no hay conexión disponible: log + retornar 503 amigable (ya hay rate limiter global que protege)
2. **Webhook Meta específicamente** — si enqueue falla por DB down, retornar 200 igual con log. Meta nos reenvía luego. NO debe retornar 500.
3. **Health checks** — `/health` debe responder 200 incluso con DB degradada (solo chequea el proceso). `/health/deep` para el chequeo DB+Redis.
4. Test: mockear DB agotado → confirmar que webhook responde 200 y no crashea el worker.

**Delegar a:** Sonnet agent — cambios puntuales en tenant_db.py + chat.py + health.py.

## 3.4 Circuit breaker para DB

**Patrón:** después de N fallos consecutivos de conexión, cortar intentos por T segundos. Fail-fast en lugar de esperar timeouts largos.

**Inspiración:** ya existe algo similar para Redis en `redis_client.py`. Replicar el patrón para DB.

**Task:**
1. En `database.py get_pool()`: tracker de fallos + timestamp último fallo
2. Si hubo N fallos en ventana T → raise rápido en lugar de intentar
3. Reset contador al primer éxito después del cooldown
4. Métrica expuesta en `/health/metrics` (`db_circuit_state: closed/open/half_open`)

**Delegar a:** Sonnet agent — archivo único + test.

---

# Orden sugerido de ataque

```
1. D1 (30 min) ─────── audit bypass staff_repo
                       └─ cheap win, cierra deuda visible

2. D2 (30 min) ─────── legacy integration tests
                       └─ no urgente, hacer si hay tiempo al final

3. 3.1 (2h)    ─────── precios por IA → Python puro
                       └─ ALTA prioridad si hay tráfico real

4. 3.3 (2h)    ─────── middleware resiliency (webhook 200 en DB down)
                       └─ ALTA si hay clientes — evita avalanchas

5. 2.1 (3h)    ─────── inventory race conditions
                       └─ MEDIA — solo se siente con tráfico alto

6. 2.2 (2h)    ─────── idempotencia Wompi
                       └─ MEDIA — depende de volumen de pagos

7. 3.2 (1h)    ─────── POS lag por LLM (solo medir primero)
                       └─ BAJA — probablemente ya resuelto con WORKER_MODE

8. 2.3 (2h)    ─────── scheduler tick leader renewal
                       └─ BAJA — rara vez pasa

9. 3.4 (1.5h)  ─────── DB circuit breaker
                       └─ BAJA — mejora operativa
```

Total: ~14–15 horas de trabajo si se hace todo. Sin urgencia real, priorizar **3.1 + 3.3** (prevención de money/flood attacks en producción).

---

# Cómo arrancar la próxima sesión

1. `git log --oneline -5` → confirmar que estás en `d166aaa` o superior.
2. `python -m alembic current` → debe dar `0030_force_rls (head)`.
3. `python -m pytest tests/ --ignore=tests/ai_sim -q` → 766 passed, 48 skipped. Si no: diagnosticar antes de seguir.
4. Leer este archivo. Decidir qué tarea tomar.
5. Si es una tarea grande (≥2h): delegar a Sonnet agent con brief bounded (patrón de esta sesión). Tu rol (Opus): planear + revisar outputs + aplicar DB.
6. Al cerrar: `git commit` con prefijo descriptivo. Actualizar este archivo marcando tareas completadas.

---

# Invariantes de Fase 1 que NO se deben romper

- `tenant_connection()` siempre corre dentro de `conn.transaction()`.
- `SET LOCAL app.restaurant_id` es parametrizado (nunca f-string).
- `mesio_superadmin` es el único role non-superuser con `BYPASSRLS`.
- `DATABASE_URL` = mesio_app (runtime). `DATABASE_URL_ADMIN` = postgres (migraciones).
- Tests mockean `app.services.database.get_pool`, no `_get_pool` (que ya no existe).
- Cualquier NUEVA tabla con `restaurant_id NOT NULL` DEBE agregarse al `_RLS_TABLES` de 0029 **Y** 0030 en una nueva migración (o se queda sin protección).

---

# Contactos internos (este repo)

- `CLAUDE.md` — contrato operativo completo. Reglas del bot. No romper.
- `app/services/tenant_context.py` — primitivas de scope. Fuente de verdad.
- `app/services/tenant_db.py` — wrapper de conexión tenant-aware.
- `alembic/versions/0027–0030` — schema hardening migrations.
- Memoria Claude (`.claude/memory/`) — contexto cross-session relevante.
