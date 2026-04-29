# Mesio — Plan de Remediación (10 Sesiones)

**Compañero:** [docs/AUDIT_REPORT.md](AUDIT_REPORT.md)
**Fecha:** 2026-04-27

---

## ⚠️ ACTUALIZACIÓN POST-2026-04-28 — Re-priorización

Las 10 sesiones originales del audit (debajo) están ~80% completadas. Status:

- ✅ Sesión 1 (`pay_check` race) — shipped commit `1a504b8`.
- ✅ Sesión 2 (endpoints sin auth) — shipped commit `aeb2428` + dead code `e5e2a08`.
- ✅ Sesión 3 (`_save_checkout_proposal`) — shipped commit `5b64dd1`.
- ✅ Sesión 4 (Meta retry unify) — shipped commit `ce92144`.
- ✅ Sesión 5 (make_reservation dedup) — shipped commit `0ee4fdf`.
- ✅ Sesión 6 (loyalty + XSS) — shipped commit `44baeab`.
- ✅ Sesión 7 (Matriz invariant cleanup) — shipped commits `e5e2a08` (dead paths) y `2fdc124` (location_id filter).
- ✅ Sesión 8 (LIMIT + tenant_connection ticket) — shipped commit `7adede3`.
- ✅ Sesión 9 (PIN login global rate-limit) — shipped commit `cdd1a1a`.
- ✅ Sesión 10 (auth fallback cleanup) — diferido (PM tiene users de prueba; ver PM_ANSWERS.md duda 10).

**Pero el testing real con PM destapó un problema MÁS GRANDE que los 10 bugs del audit**: el flujo de mesa end-to-end NO está conectado. Diez disconnects entre módulos operativos (bot ↔ mesa ↔ cocina ↔ mesero ↔ caja ↔ factura ↔ NPS) — ninguno cubierto por los 1170+ tests existentes porque los tests son unitarios, no E2E.

**Próximas sesiones tienen prioridad cambiada:**

### SESIÓN 0 — End-to-End Flow Test (NUEVA, ARRANCA AQUÍ)

**Por qué importa (negocio):** sin un test que cubra el flujo entero, cualquier feature nueva agrega más bugs. PM probó hoy el flujo y vio que aunque las piezas individuales pasan tests, las conexiones entre módulos tienen leaks. La prioridad #1 es **construir un E2E test que falle meaningfully** y use sus failures como roadmap concreto.

**Entregable:**
- `tests/test_e2e_table_flow.py`: integración real (no mocks salvo Anthropic/Meta/Wompi) que cubra:
  1. POST /api/qr-claim (cliente entra teléfono).
  2. Bot recibe primer mensaje (mocked Meta webhook).
  3. detect_table_context Path 0 abre table_session.
  4. Bot procesa item del menú → crea table_order vinculado a la mesa.
  5. Cocina ve la orden via /api/table-orders?station=kitchen.
  6. Cocina marca status=listo via POST /api/table-orders/{id}/status.
  7. Mesero recibe alerta (waiter_alert) automática.
  8. Mesero marca status=entregado.
  9. Caja factura via pay_check.
  10. NPS dispara.
  11. table_session.verified, factura_entregada, NPS sent — todos true.

**Resultado esperado**: el test FALLA en múltiples puntos. Cada falla es uno de los 10 disconnects de PRODUCT_CONTEXT.md regla #13.

**Esfuerzo:** M-L (4-6h)
**Riesgo de romper:** BAJO — el test es nuevo, no toca código existente.

### SESIONES 0a-0j — Disconnect fixes (10 sub-sesiones)

Una sesión por disconnect, en el orden que el E2E test los hace fallar. Cada sub-sesión:
1. Reproduce el disconnect aislado (test rojo).
2. Implementa el fix.
3. Test rojo → verde.
4. Smoke E2E completo (asegurar que no rompió otro disconnect ya arreglado).
5. Commit + push.

Lista de disconnects en PRODUCT_CONTEXT.md regla #13. Resumida acá:

| # | Disconnect | Estimado | Bloquea sesiones futuras |
|---|---|---|---|
| 1 | Bot ↔ Mesa: items huérfanos cuando QR-claim falla | M (3h) | Capa 2/3 |
| 2 | Mesa ↔ Mesero: assigned_staff_id sin UI | M (4h) | Notificaciones cocina-mesero |
| 3 | Cocina ↔ Mesero: alerta `listo` faltante | S-M (2-3h) | Cierre de ciclo |
| 4 | "Marcar entregada" UI faltante | S (2h) | Cierre de ciclo |
| 5 | Caja ↔ Bot: chats activos no visibles a mesero | M (4h) | UX premium |
| 6 | Bot ↔ Cocina: multi-curso secuencial | M (5h) | Restaurantes con menú largo |
| 7 | Mesero: tab "Pedidos activos" | S (2h) | UX |
| 8 | NPS timing: dispara antes de factura_entregada | S (1h) | Tasa de respuesta NPS |
| 9 | Reservas → mesa auto-asignar | M (4h) | Reservas | 
| 10 | Multi-orden por mesa (Capa 2 join_code) | L (1.5d) | Familia/grupo |

### SESIÓN POST-DISCONNECTS — Capas 2 y 3 originales

Una vez los 10 disconnects están verdes, retomar [docs/MESA_QR_ARCHITECTURE.md](MESA_QR_ARCHITECTURE.md):
- Capa 2: Multi-participante con join_code.
- Capa 3: Anti-impostor con validación primer pedido por mesero.

Estas capas asumen flujo base verde. Sin disconnects resueltos, agregarlas suma capas a un piso roto.

---

## Cómo leer este plan

- **Orden de prioridad:** primero lo que evita que el restaurante deje de operar, después lo que protege ingresos, después lo que limpia deuda.
- **Esfuerzo:** S = <2h, M = 2-6h, L = 6-12h.
- **Riesgo de romper algo:** lo que importa cuando arreglás. ALTO = puede tumbar el bot al desplegar; MEDIO = afecta features visibles; BAJO = cambios aislados.
- **Test mínimo:** lo que DEBE pasar antes de mergear. No es la suite completa — es el guardarraíl mínimo.
- Cada sesión asume que comenzás con `git checkout main && git pull && git checkout -b session-N-slug`.

---

## SESIÓN 1 — Doble cobro en `pay_check` (TOCTOU + race)

**Por qué importa (negocio):** Dos cajeros pagando el mismo check en la misma ventana de 10s producen DOS facturas DIAN, doble loyalty acumulada, y `payments` JSON sobreescrito. El cliente es facturado de más, la propina puede duplicarse, y si la DIAN cobra por factura emitida, hay costo directo a Mesio. Es **el bug más probable de explotar primero** en un restaurante con dos terminales caja.

**Archivos:**
- `app/repositories/tables_repo.py:797-808` (`db_finalize_check_payment`)
- `app/routes/tables.py:1278-1390` (`pay_check`)

**Cambio principal (sin escribir código aún):**
- Agregar `AND status='open'` al WHERE del UPDATE.
- Verificar `cmd_status` (rowcount). Si == 0 → 409 "Check ya pagado".
- Envolver `db_get_check` + `db_finalize_check_payment` en `async with conn.transaction()` con `SELECT FOR UPDATE` sobre el row del check.
- Considerar bajar el rate-limit de `pay_check` (hoy 3/10s) a 1/15s por check_id.

**Esfuerzo:** M (2-3h)
**Riesgo de romper:** MEDIO — Caja es path crítico. Riesgo principal: introducir deadlocks si la TX se mantiene abierta durante la inserción fiscal_invoice.
**Test mínimo:**
1. Test integration: dos `pay_check` simultáneos al mismo check_id → uno gana, el otro recibe 409.
2. Test integration: pay_check con check ya en `status='paid'` → 409 explícito.
3. `tests/test_salon_split_checks_lifecycle.py` sigue pasando.
4. Smoke manual en TEST_DATABASE_URL: pagar mesa de 2 checks individualmente, verificar fiscal_invoices = 2 (no 4).

---

## SESIÓN 2 — Endpoints sin auth tocando dinero / estado del bot

**Por qué importa (negocio):** Tres endpoints públicos que **no deberían serlo**:

1. `POST /cart/clear` — cualquiera borra el carrito de cualquier cliente del bot. Sabotaje gratis. Contradice Regla #5 de cart locks.
2. `GET /payment/confirm?id=` — expone totales de pedidos por order_id. Si tus order_ids son adivinables/secuenciales, es leak financiero.
3. `GET /api/public/restaurant-info?id=` — enumera nombres de restaurantes por org_id. Ayuda a atacante post-Wave-2 a encontrar IDs válidos para spoofing de header.

**Archivos:**
- `app/routes/orders_routes.py:70-73` (`/cart/clear`)
- `app/routes/orders_routes.py:249-275` (`/payment/confirm`)
- `app/routes/dashboard.py:238` (`/api/public/restaurant-info`)

**Cambio principal:**
- `/cart/clear`: **PRIMERO confirmar con PM quién lo usa** (ver Duda #5). Si es el bot mismo, eliminar y pasar a internal. Si es el frontend público, agregar token de ownership por carrito (UUID emitido al crear cart).
- `/payment/confirm`: requerir token o JWT, o convertir a redirect que solo confirma sin exponer datos.
- `/api/public/restaurant-info`: pasar a slug-based (`/r/{slug}/info`) y eliminar la versión por id.

**Esfuerzo:** S+M (3h, depende de cuán enredado esté `/cart/clear`)
**Riesgo de romper:** MEDIO — `/cart/clear` puede tener clientes invisibles. **Pre-requisito: revisar 30 días de logs Railway** para ver volumen de hits y origen.
**Test mínimo:**
1. Cada uno de los 3 endpoints sin auth → 401/403.
2. Con auth válido → flujo normal.
3. Smoke manual: bot real, agregar items, llamar `/cart/clear` con token correcto, verificar limpieza.

---

## SESIÓN 3 — Cliente Meta unificado + timeouts en httpx

**Por qué importa (negocio):** El bot pierde mensajes hoy en silencio. Cuando Meta devuelve un 503 transiente:

- `meta_api.send_text` reintenta 3× → mensaje llega ✅
- `chat._send_wa_text` (1 intento, 10s) → mensaje SE PIERDE ❌

`_send_wa_text` se usa para mensajes críticos: rate-limit warning, audio fallback, "comprobante recibido". Y aparte, `chat.py:133 /api/media/{id}` no tiene timeout: una hora pico con muchos comprobantes + Meta lento puede starvar el pool y tumbar el web service entero.

**Archivos:**
- `app/routes/chat.py:133` (media proxy sin timeout)
- `app/routes/chat.py:190` (single 10s send AI reply)
- `app/routes/chat.py:276-292` (`_send_wa_text`)
- `app/services/meta_api.py` (cliente correcto)

**Cambio principal:**
- Reemplazar todas las llamadas Meta de `chat.py` por `meta_api.send_text` / `meta_api.get_media`.
- Si `meta_api` no tiene `get_media`, agregarlo con timeout 15s y 2 retries para 5xx.
- Borrar `_send_wa_text` y migrar callers.
- Agregar timeout=15 a TODOS los `httpx.AsyncClient` del proyecto (un grep + revisión).

**Esfuerzo:** M (3-4h)
**Riesgo de romper:** MEDIO — Cambiar el cliente Meta puede modificar headers/payloads sutilmente. Test contra Meta sandbox antes de prod.
**Test mínimo:**
1. Mock Meta returning 503 once → assertion: 2do intento ocurre.
2. Mock Meta lento (10s+ delay) → request termina con timeout, no cuelga.
3. Smoke manual: voice note → audio fallback message llega aunque Meta tire 503 una vez.

---

## SESIÓN 4 — Idempotencia y race en loyalty + Wompi callback

**Por qué importa (negocio):** Los puntos de fidelidad **se pueden duplicar** hoy bajo dos condiciones normales:
1. Wompi reenvía un evento (común — Wompi reintenta en timeouts de Cloudflare).
2. Cajero "valida manualmente" un pedido y luego llega callback Wompi.

Combinado, los puntos pueden multiplicarse 2× a 4×. El programa de loyalty está **regalando dinero** y el reporte mensual lo refleja como "engagement creciente". 🔥

**Archivos:**
- `app/repositories/loyalty_repo.py:71-110` (`db_accrue_loyalty_points`)
- `app/routes/orders_routes.py:106-247` (Wompi webhook)
- `app/routes/orders_routes.py:400-522` (`validate_delivery_order` manual)

**Cambio principal:**
- Agregar UNIQUE INDEX en `loyalty_ledger(org_id, order_id) WHERE delta > 0`. Migración nueva.
- En `db_accrue_loyalty_points`, cambiar el flujo a `INSERT ... ON CONFLICT DO NOTHING RETURNING id` y solo si retorna fila, actualizar `loyalty_customers.points_balance`.
- Cambiar parámetro `total_cop: float` → `total_cop: Decimal`.
- En `validate_delivery_order`, registrar el synthetic id en la misma tabla `wompi_events` para que un Wompi callback posterior haga short-circuit.

**Esfuerzo:** M-L (4-6h con migración + tests)
**Riesgo de romper:** MEDIO — Migraciones tocan loyalty_ledger en prod. Reherse contra TEST_DATABASE_URL.
**Test mínimo:**
1. Test integration: dos accrue_loyalty con mismo order_id concurrente → solo 1 ledger row, balance = 1× delta.
2. Test integration: Wompi webhook + manual validate → solo 1 accrue total.
3. Test: parámetro Decimal pasa, float también pasa (compatibilidad temporal con coerción).

---

## SESIÓN 5 — Watchdog para scheduler + alerta Anthropic + bug import

**Por qué importa (negocio):** El scheduler puede MORIR sin que nadie se entere. `app/main.py:78-79` lanza `asyncio.create_task` sin guardar la task ni `done_callback`. Si tira excepción, GC la limpia y los recordatorios de reserva, weekly reports y occupancy snapshots dejan de salir. Hoy mismo hay un bug confirmado: `scheduler.py:252` referencia `bypass_tenant_scope` sin importarlo → NameError silencioso → occupancy_snapshots **rotos para todos los tenants** desde hace tiempo.

Y aparte: si Anthropic está ratoneando 529s 30 minutos seguidos, el bot va lento, los clientes se quejan, y nadie tiene una alerta automática.

**Archivos:**
- `app/main.py:78-90` (lifespan / start_scheduler)
- `app/services/scheduler.py:252` (NameError)
- `app/services/scheduler.py` (importar bypass_tenant_scope correcto)
- `app/services/alerts.py` (agregar check Anthropic)
- `app/services/agent.py:call_claude` (loguear tokens_in/out + latencia)

**Cambio principal:**
- Guardar `_scheduler_task = asyncio.create_task(...)` en main.py + `add_done_callback(handle_scheduler_died)` que loguee CRITICAL + dispare alerta vía webhook.
- Importar `bypass_tenant_scope` correctamente en scheduler.py:252.
- Agregar contador en `agent.call_claude`: si >50% de calls últimos 5min son 5xx → alerta MEDIUM "Anthropic degradado". Use `state_store.rate_limit_check` o un contador en Redis.
- Loguear tokens_in/tokens_out/latency en `call_claude` (copiar patrón de `ai_insights.py:154`).

**Esfuerzo:** M (3-4h)
**Riesgo de romper:** BAJO — Cambios aditivos, no tocan paths críticos.
**Test mínimo:**
1. Test unitario: monkeypatch `_scheduler_loop` para que tire `RuntimeError` → assertion: callback dispara, log CRITICAL emitido, alert webhook llamado.
2. Smoke contra TEST_DATABASE_URL: occupancy_snapshots se pueblan después del fix.
3. Test: 10 calls Claude con response 529 → alerta dispara una vez.

---

## SESIÓN 6 — Race en JSONB writes (menu, features) + lock optimista

**Por qué importa (negocio):** Dos admins editando el menú simultáneamente — cosa que pasa cuando el dueño y el gerente están en el mismo restaurante con dos laptops, o cuando alguien deja la pestaña abierta y otro también edita — produce **last-write-wins silencioso**. Lo mismo para `features` (config de tip distribution, currency, módulos activos). Catálogo v2 con uploads de imagen + cambios de precio simultáneos = exactamente este escenario.

**Archivos:**
- `app/repositories/restaurant_repo.py:1256` (`db_update_menu`)
- `app/repositories/restaurant_repo.py:289, 296, 445` (features writes)
- `app/repositories/loyalty_repo.py:90-100` (race en accrue: parcialmente solucionado en Sesión 4 — verificar)

**Cambio principal:**
- Agregar columna `menu_version BIGINT DEFAULT 1` y `features_version BIGINT DEFAULT 1` a `organizations` (migración nueva).
- `db_update_menu(restaurant_id, menu_data, expected_version)` → `UPDATE ... SET menu = $1, menu_version = menu_version + 1 WHERE id = $2 AND menu_version = $3`. Retorna False si rowcount == 0.
- Frontend lee version, manda en PATCH, si conflict → toast "alguien más editó, refresca".
- Mismo patrón para features.

**Esfuerzo:** M-L (5-6h con frontend)
**Riesgo de romper:** MEDIO — Toca schema (migración) + frontend. Plan rollback: tolerar `expected_version=None` por 1 release como deprecation grace.
**Test mínimo:**
1. Test integration: dos updates concurrentes con misma version → uno gana, otro 409.
2. Test: update con version stale → 409 con detalle.
3. Test: update sin version (legacy clients) → tolerar pero loguear warning durante grace period.

---

## SESIÓN 7 — Matriz invariant trick + 4 caminos a current_restaurant

**Por qué importa (negocio):** CLAUDE.md §9 prohíbe expresamente la "Matriz invariant trick" (`restaurant.get("location_id") or org_id`) — pero **5 sitios productivos siguen usándola** ([docs/AUDIT_REPORT.md §3](AUDIT_REPORT.md#3-inconsistencias-de-patrón)). Para Orgs creadas DESPUÉS del deploy de Wave-2, `org_id` y `location_id` son auto-incrementos independientes. La trick devuelve location equivocado → reportes y operaciones leen datos cruzados internamente. Es deuda silenciosa que se acumula con cada nuevo cliente onboardeado.

Aparte, hay 4 formas de resolver el restaurante actual (`get_current_restaurant`, `_scoped`, `get_current_org`, `_scoped`). 46 call-sites usan la versión plana sin tenant_scope.

**Archivos:**
- `app/routes/stats.py`, `app/routes/tables.py`, `app/routes/settings_routes.py`
- `app/repositories/orders_repo.py`, `app/repositories/tables_repo.py`
- `app/routes/deps.py` (consolidación)

**Cambio principal:**
- Eliminar las 5 ocurrencias de `restaurant.get("location_id") or org_id`.
- En su lugar: usar `restaurant["location_id"]` directo (siempre populado post-Paso 7 de Wave-2). Si es None, query explícita `SELECT id FROM locations WHERE org_id = $1 ORDER BY id ASC LIMIT 1` (NO "primary").
- Migrar 10 call-sites de `get_current_restaurant` plano a `get_current_restaurant_scoped` por iteración (no todos a la vez).
- Agregar test AST-walking en `tests/test_no_matriz_trick.py` que falle CI si reaparece el patrón.

**Esfuerzo:** L (6-8h, mucho test)
**Riesgo de romper:** MEDIO-ALTO — toca paths leídos por dashboard, stats, tables. Hacer en branch + soak en TEST_DATABASE_URL.
**Test mínimo:**
1. Test específico: org creado con `org_id=42, location_id=100` no-igual → todos los endpoints retornan datos del location correcto.
2. Suite completa post-Wave-2: 1170 passed sigue passing.
3. Lint AST: cero ocurrencias del trick.

---

## SESIÓN 8 — Fix `db_get_delivery_status_hash` + bypass leak en kitchen

**Por qué importa (negocio):** **Cualquier kitchen page autenticada de cualquier restaurante está leyendo IDs de pedidos delivery de TODOS los demás tenants Mesio** vía polling cada 10s. Es el peor leak cross-tenant que tienes hoy. Existe ya la versión correcta (`db_get_delivery_status_hash_for_restaurant`) — solo está mal cableada.

Aparte: 4-5 `bypass_tenant_scope` en `staff_repo.py` están keyed solo por UUIDs cliente-supplied. Necesitan defensa en profundidad (cross-check post-lookup).

**Archivos:**
- `app/repositories/tables_repo.py:1409-1414` (función mala)
- `app/repositories/tables_repo.py:1418` (función buena)
- `app/routes/tables.py:511` (call site)
- `app/repositories/staff_repo.py:944, 1039-1102` (4 bypasses keyed por UUID)
- `app/routes/staff.py:947` (export_payroll_run — verificar IDOR)

**Cambio principal:**
- Cambiar el call-site en tables.py:511 a la versión `_for_restaurant` con el org_id del request.
- Borrar la versión global no-scoped (o renombrarla `_internal_only` con error si se llama desde rutas tenant-scoped).
- Para los 4 bypasses en staff_repo: agregar verificación post-fetch que el row retornado pertenezca al `staff_id` claim del JWT (defensa profundidad).
- Verificar `db_get_payroll_run` filtra por `org_id` además de `run_id`. Si solo filtra por `run_id`, agregar.

**Esfuerzo:** M (3-4h con tests cross-tenant)
**Riesgo de romper:** BAJO-MEDIO — Cambios localizados.
**Test mínimo:**
1. Test integration cross-tenant: org A tiene pedidos, org B abre kitchen page → org B no ve IDs de A.
2. Test: WebAuthn credential de tenant A no se puede usar para auth en tenant B.
3. Test: `export_payroll_run` con run_id de otro tenant → 403/404.

---

## SESIÓN 9 — XSS en loyalty + lint frontend extendido + caja silent failures

**Por qué importa (negocio):** Tres bugs frontend que afectan distintos perfiles de usuario:

1. **XSS stored vía teléfono de cliente loyalty** (dashboard-components.js:2430). Si un cliente registra un teléfono con HTML inyectado por el bot (vector indirecto pero existente), exfiltra el JWT del owner. Compromise total.
2. **Caja `loadMenu` y `_loadBillingConfig` tragan errores** (caja.js:53, 72). Cajero abre POS, falla red, ve UI vacía. Procesa pagos con config stale.
3. **Lint frontend no chequea XSS** — una sola regex en `scripts/lint_frontend.py` cubriría el #1 a futuro.

**Archivos:**
- `app/static/js/dashboard-components.js:2430-2434, 2459-2460, 493`
- `app/static/js/pages/caja.js:53, 72`
- `app/static/js/dashboard-core.js:120`
- `scripts/lint_frontend.py`
- `tests/test_frontend_lint.py`

**Cambio principal:**
- Reemplazar `innerHTML = '...${var}'` con patrón seguro (textContent + appendChild) o usar `_escHtml(var)` de mesio-utils.
- En caja: mostrar toast de error en lugar de tragar.
- Agregar regla al linter: `/\.innerHTML\s*=\s*[`'\"][^`'\"]*\$\{[^}]+\}/` (con allowlist `// lint-allow:` para casos justificados).
- Pasar lint sobre todo el JS — fixear lo que reaparezca.

**Esfuerzo:** M (3-4h)
**Riesgo de romper:** BAJO — solo frontend.
**Test mínimo:**
1. `python scripts/lint_frontend.py` con regex nueva activa → 0 violaciones.
2. Manual: abrir loyalty page con un teléfono que incluya `<img src=x>` → renderiza como texto plano.
3. Manual: caja con red caída → toast aparece, no UI vacía.

---

## SESIÓN 10 — Limpiar duplicados + dead code + checkout extract

**Por qué importa (negocio):** No es negocio directo, es **velocidad futura**. Hoy todo cambio en checkout requiere editar 2-3 archivos (agent.py + agent_salon.py). Cada bug fix tiene que aplicarse dos veces y a veces solo se aplica a uno. Con cada nueva línea duplicada, el costo de la próxima sesión sube.

Lista concreta:
- `_fmt_cop` definido en agent.py + agent_salon.py.
- `_ofuscar_phone` definido en agent.py + agent_salon.py.
- Checkout flow ~40 LOC duplicados entre agent.py y agent_salon.py.
- 6 funciones `db_init_*` no-op llamadas inútilmente.
- `is_main_restaurant` y `is_primary` parámetros vestigiales.
- Reads de `parent_restaurant_id` que siempre dan None (5 sitios).
- Migración 0008 con filename engañoso.
- `_ensure_usage_table` no-op que se llama por cada increment.
- 26 catches vacíos en JS.

**Archivos:** múltiples (ver lista en [docs/AUDIT_REPORT.md §3](AUDIT_REPORT.md#3-inconsistencias-de-patrón) — sección "Duplicaciones brutales" y "Dead code residual")

**Cambio principal:**
- Extraer `app/services/agent_helpers.py` con `_fmt_cop`, `_ofuscar_phone`, `_build_checkout_menu`, `_parse_checkout_selection`. Importar desde los 3 agentes.
- Borrar las 6 funciones `db_init_*` no-op + sus call sites.
- Borrar lecturas dict de `parent_restaurant_id` (siempre None post-0038).
- Borrar parámetro `is_main_restaurant` de `db_create_location` y callers.
- Renombrar `0008_checkout_proposals.py` → `0010_checkout_proposals.py` (filename only, revision sigue siendo 0010).
- Auditar 26 catches vacíos JS, agregar log + toast donde corresponda.

**Esfuerzo:** L (8-10h, mucho cuidado en grep cross-repo)
**Riesgo de romper:** ALTO si se hace mal — toca código del bot. Hacer en sub-sesiones de 1 cleanup por commit.
**Test mínimo:**
1. Suite completa pasa post-cleanup (1170 passed mantenido).
2. Smoke E2E: salon checkout flow + delivery checkout flow + pickup checkout flow funcionan iguales.
3. `git grep parent_restaurant_id app/` → solo coincidencias en alembic + tests + comments.
4. `git grep _fmt_cop app/services/` → 1 sola definición.

---

## Apéndice — Lo que NO está en este plan (y por qué)

### Cosas que NO hacer ahora (deuda real pero menor)

- **Test claim-then-ack 3-fase para inbox** — Importante para regresión, pero el patrón ya está estable y el riesgo de regresión real es bajo si nadie toca `inbox_worker.py`. Agregar después de Sesión 5.
- **End-to-end QR → mesa → cocina → caja test** — Largo de escribir bien (~1 día). Vale la pena cuando estabilicemos el resto.
- **Correlation ID end-to-end (webhook → DB)** — Excelente para postmortems pero no urgente. Diferir hasta tener ≥10 tenants activos.
- **Log shipping (Datadog/Loki)** — Costo recurrente. Defer hasta tener ingresos que lo justifiquen.
- **Per-restaurant operational dashboard** — UX premium, no afecta operación. Diferir.
- **N+1 perf debt** (`db_calculate_payroll`, `db_get_restaurant_detail_stats`) — Mejorar cuando algún tenant esté arriba de 100K orders. Hoy no es bottleneck.
- **Refactor staff_repo.py 96KB / restaurant_repo.py 99KB** — God files reales pero no rompen nada. Costo de cambio > beneficio inmediato.

### Cosas que NO hacer NUNCA (el PM ya las descartó)

- Reescribir en otro framework.
- Microservicios.
- Event sourcing.
- Kubernetes.

### Pre-requisitos que el PM debe resolver

Ver [docs/AUDIT_REPORT.md → DUDAS PARA EL PM](AUDIT_REPORT.md#dudas-para-el-pm) — 10 preguntas. Sin respuesta a #4, #5, #7 no puedo cerrar Sesión 2 con confianza.

---

## Cómo correr cada sesión (proceso sugerido)

1. Pegame este markdown + el AUDIT_REPORT y dime "vamos por sesión N".
2. Yo leo los archivos exactos del scope antes de proponer diff.
3. Te muestro el plan concreto del cambio (no diff todavía).
4. Vos aprobás o course-correct.
5. Yo escribo el código + tests.
6. Vos corrés `pytest` con TEST_DATABASE_URL local.
7. Si pasa, mergeás.

**Tiempo total estimado del plan:** ~40-50h de sesiones efectivas, distribuidas en 2-4 semanas según ritmo.

**Ganancia neta:** elimina los 12 bugs 🔥, reduce duplicación ~200 LOC, agrega watchdog operacional, cierra leaks cross-tenant, y deja la base lista para que el bot escale a 50 clientes sin que la deuda silenciosa explote.
