# PM_ANSWERS.md — Resoluciones de las 10 dudas técnicas del audit

**Generado:** 2026-04-27
**PM:** Miguel (solo founder, no programador)
**Método:** entrevista 1-a-1 con hipótesis del agente + confirmación del PM
**Pre-requisito de:** todas las sesiones del REMEDIATION_PLAN.

---

## Duda 1 — Doble path de routing en `agent_external.py`

**Pregunta original:** *"`agent_external.py:295-337` vs `:340-371`. Hay dos paths paralelos para resolver delivery: 'Org-based' y 'legacy restaurants'. CLAUDE.md dice Wave-2 está completo. ¿Por qué siguen ambos? ¿El 'legacy' se ejecuta alguna vez en producción?"*

**Contexto en código:**
- [agent_external.py:294-371](../app/services/agent_external.py): delivery branch con `_org_location_resolved` flag → fallback a "legacy" path si False.
- [agent_external.py:374-507](../app/services/agent_external.py): pickup branch idéntica con `_pickup_org_resolved`.
- "Legacy" no es pre-Wave-2 — ya joinea `locations` y filtra por `org_id`. Es solo un segundo intento con SQL distinto.

**Respuesta del PM:** *"Sí, debería deletearse."*

**Implicación para el plan:**
- Sesión "dead code" elimina los dos legacy paths (~200 LOC totales: líneas 339-371 + 433-507).
- **Pre-flight obligatorio en producción** antes del delete:
  ```sql
  SELECT o.id, o.name FROM organizations o
  LEFT JOIN locations l ON l.org_id = o.id
  WHERE l.id IS NULL;
  ```
  Si retorna 0 filas → safe delete. Si retorna ≥1 → fix la org-orfan primero.

**Acciones pendientes:** correr query orphan-orgs en producción antes de mergear el delete.

---

## Duda 2 — Tabla `tip_distributions`

**Pregunta original:** *"CLAUDE.md dice 'legacy, no se usa para cálculo activo' pero `db_save_tip_distribution` y `db_get_tip_distributions` siguen ahí. ¿Algún cliente legacy todavía lee de esa tabla? ¿Se puede borrar?"*

**Contexto en código:**
- [staff_repo.py:824](../app/repositories/staff_repo.py): `db_save_tip_distribution` — **cero callers en `app/`**. Truly dead.
- [staff_repo.py:857](../app/repositories/staff_repo.py): `db_get_tip_distributions` — un caller: [staff.py:413](../app/routes/staff.py) (endpoint GET admin del histórico).
- Cálculo activo de propinas usa `db_calculate_tips_by_attendance` (sin tocar `tip_distributions`).

**Respuesta del PM:** *"También puede borrarse. No hay riesgos."*

**Implicación para el plan:**
- Sesión "dead code" hace 4 cosas:
  1. Drop función `db_save_tip_distribution` (cero callers).
  2. Drop endpoint `GET /api/staff/tip-distributions`.
  3. Drop función `db_get_tip_distributions`.
  4. Drop UI panel admin que lo consume.
  5. Migración Alembic `DROP TABLE tip_distributions`.

**Acciones pendientes:** ninguna.

---

## Duda 3 — `legacy_redirects.py`

**Pregunta original:** *"¿Tienes acceso a logs de Railway de los últimos 30 días para ver si `internal.legacy_url` sigue disparando? Si zero hits → safe delete."*

**Contexto en código:**
- [legacy_redirects.py](../app/routes/legacy_redirects.py): 89 LOC, 7 redirects 301 hacia `/internal/*`.
- **Único frontend que apunta a URLs viejas:** [superadmin.html:190-191](../app/static/html/internal/superadmin.html) → `href="/analytics"` y `href="/monitoring"`.

**Respuesta del PM:** *Confirmado borrar. Acopla con actualizar los hrefs del superadmin a `/internal/analytics` y `/internal/monitoring` directo.*

**Implicación para el plan:**

Sesión "dead code" hace cambio acoplado en un solo commit:
1. Editar `superadmin.html:190-191` → `href="/internal/analytics"` y `href="/internal/monitoring"`.
2. Borrar `app/routes/legacy_redirects.py` (89 LOC).
3. Quitar el `app.include_router(legacy_redirects.router)` en `main.py`.
4. **Mantener** `routes/internal/analytics.py` + `routes/internal/ops.py` + sus HTMLs y endpoints (PM los quiere vivos para monitorear cuando haya tracción).

**Acciones pendientes:** ninguna.

---

## Duda 4 — `/api/public/restaurant-info?id=`

**Pregunta original:** *"¿De dónde se llama? Lo encontré sin auth y enumera nombres de restaurantes por ID. ¿Es para landing pages? ¿Se puede mover a `/r/{slug}/info` con slug en lugar de ID numérico?"*

**Contexto en código:**
- [dashboard.py:238-244](../app/routes/dashboard.py): endpoint público sin auth, retorna solo `{"name": ...}`.
- **Único caller:** [login.html:742](../app/static/html/login.html) — pinta "Restaurante: La Trattoria" arriba del form de login.

**Respuesta del PM:** *Opción B (slug). "Hacer las cosas bien desde el principio." También corrigió mi mental model: hoy NO existe el flujo "crear restaurante → auto-crear owner user → mandar link de login".*

**Implicación para el plan:**

Sesión 4 (slug migration):
1. Migración Alembic: agregar `organizations.slug TEXT UNIQUE NOT NULL`. Backfill desde `name` con sufijo numérico para colisiones.
2. Endpoint nuevo: `GET /api/public/restaurant-info?slug=la-trattoria-bogota`. Borrar el viejo basado en `id`.
3. Cambiar `?r=42` → `?r=la-trattoria-bogota` en los 6 sitios:
   - [dashboard.py:110](../app/routes/dashboard.py)
   - [login.html:742](../app/static/html/login.html)
   - [dashboard-components.js:949](../app/static/js/dashboard-components.js)
   - [mesio-utils.js:118](../app/static/js/mesio-utils.js)
   - [roles.js:7](../app/static/js/roles.js)
   - [staff-clock.js:1466](../app/static/js/pages/staff-clock.js)
4. Rate-limit defensivo: 30 req/min/IP en el endpoint nuevo.

**Acciones pendientes (NUEVA tarea fuera de las 10 originales):**
- Sesión nueva: **"Owner bootstrap workflow"** en superadmin. Hoy crear restaurante NO crea usuario admin ni link de login. Falta UI/endpoint para que PM termine la configuración con un botón "Crear usuario owner" → genera credencial → la copia para mandar al cliente. Prioridad alta para primer onboarding real.

---

## Duda 5 — `/cart/clear` sin auth

**Pregunta original:** *"¿Quién lo usa? ¿Es para que el bot mismo limpie via internal? Si el bot lo llama, debería ir por `bypass_tenant_scope` server-side, no via HTTP público."*

**Contexto en código:**
- [orders_routes.py:70-73](../app/routes/orders_routes.py): endpoint público sin auth.
- **Callers frontend:** ZERO en `app/static/**`.
- **Callers backend:** `agent_salon.py` llama `orders.clear_cart()` directo (función Python), NO al endpoint HTTP.
- **Hallazgo extra:** el menú web público (`/menu`) tiene un carrito separado que vive en `localStorage` del browser ([catalog-v2.js:139-152](../app/static/js/catalog-v2.js)), NO toca BD.

**Respuesta del PM:** *Borrar el endpoint HTTP. Y agregar dos features nuevas: botón "Vaciar carrito" en el menú web (puro localStorage, frontend) + tool `clear_cart` en el bot WhatsApp (con confirmación obligatoria del bot, intent recognition estricto).*

**Implicación para el plan:**

| Acción | Sesión |
|---|---|
| Borrar `POST /api/cart/clear` + Pydantic + test smoke | Sesión 2 (auth) |
| Botón "Vaciar carrito" en `/menu` (frontend localStorage) | Sesión nueva (UX menú) |
| Tool `clear_cart` en bot WhatsApp con confirmación + dedup | Sesión nueva (bot features) |

Tool `clear_cart` requiere:
- Definition en `agent_tools.py` con descripción explícita "only invoke when user EXPLICITLY asks to empty/reset cart".
- Handler en `agent.py _validate_tool_call` + `execute_action`.
- **Confirmación obligatoria del bot antes de ejecutar:** "¿Seguro vacío el carrito? Tienes X items por $Y. (sí / no)".
- Dedup guard (agregarlo a `_ORDER_TOOLS` o equivalente).
- Log estructurado `cart_cleared_by_user_request` para auditoría.

**Acciones pendientes:** ninguna del scope original. Las 2 nuevas tareas se agregan al plan.

---

## Duda 6 — `pos_quick_invoice` no decrementa inventario

**Pregunta original:** *"¿Es por diseño (caja vende cosas que no pasan por el menú: 'consumo cocina', 'venta varia')? Si sí, OK. Si es bug, hay que arreglar."*

**Contexto en código:**
- [tables.py:1530-1591](../app/routes/tables.py): endpoint recibe items con `name + unit_price + qty` arbitrarios (string libre, no SKU lookup), crea order+check+pago en un shot.
- NO toca `inventory.stock` en ningún momento. Compará con [orders_repo.py commit_order_transaction](../app/repositories/orders_repo.py) que sí decrementa.

**Respuesta del PM:** *Refinó el diseño. Quick-invoice debe permitir items del menú (con descuento de inventario normal) Y items personalizados con flag explícito. Visual admin para auditoría de "facturas con items no-trazables".*

**Implicación para el plan:**

Sesión nueva: **"Quick-invoice menú-first + items personalizados auditables"**. Esfuerzo medio-alto.

Cambios técnicos:
1. Migración Alembic: `ALTER TABLE table_orders ADD COLUMN has_custom_items BOOLEAN DEFAULT false` + índice parcial `WHERE has_custom_items = true`.
2. Backend: refactor `pos_quick_invoice` para procesar items en dos buckets (menú vs custom). Items menú-matched invocan `commit_order_transaction` (decrementa stock). Items custom NO. Si TODOS son menú-matched, comportamiento idéntico al bot (zero diff). Si HAY al menos uno custom, flag `has_custom_items=true`.
3. Frontend: rediseño del modal quick-invoice en `caja.html/js`:
   - Picker del menú primero (grid/buscador con SKUs reales).
   - Botón secundario "+ Item personalizado" → modal con warning destacado: *"⚠️ Este ítem no descontará inventario. Solo úsalo para ventas que no pasan por el menú formal (consumo staff, venta varia, etc.). Quedará registrado para auditoría."*
4. Endpoint nuevo: `GET /api/pos/orders/with-custom-items?date_from=&date_to=` para visual admin.
5. Repo nuevo: función en `tables_repo.py` para query de auditoría con índice eficiente.
6. Visual admin: nueva pestaña/filtro en `/pedidos` o `/billing` llamada **"Facturas con items no-trazables"** con filtros por sucursal, cajero, rango de fechas + export CSV.
7. Tests: caso menú-only, caso mixed, caso custom-only, caso menú con out-of-stock.

**Acciones pendientes:** ninguna.

---

## Duda 7 — `X-Branch-ID = "all"` post-Wave-2

**Pregunta original:** *"CLAUDE.md menciona que retorna Matriz + Sucursales. ¿En qué contextos se usa 'all'? ¿Es seguro permitirlo después de Wave-2?"*

**Contexto en código:**
- Header `X-Branch-ID="all"` lo manda el frontend cuando el owner selecciona "Todas las sucursales" en dropdown.
- [dashboard-core.js:181-185](../app/static/js/dashboard-core.js): bloquea 3 secciones del UI cuando es "all" (`menu`, `mesas`, `staff`).
- Backend: ~10 sitios leen el header. Stats/NPS agregan cross-location dentro del mismo org. Settings cross-location solo para owners/admins.

**Respuesta del PM:** *"Sí, no es bug. Sí, se debe suprimir de todas partes la matriz. El all es solo un filtro visual."*

**Implicación para el plan:**

Sesión 7 ataca **solo la "Matriz invariant trick"** en los 5 sitios productivos:
- [routes/stats.py](../app/routes/stats.py)
- [routes/tables.py](../app/routes/tables.py)
- [routes/settings_routes.py](../app/routes/settings_routes.py)
- [repositories/orders_repo.py](../app/repositories/orders_repo.py)
- [repositories/tables_repo.py](../app/repositories/tables_repo.py)

Reemplazar `restaurant.get("location_id") or org_id` por:
- Si el dict viene de `db_get_restaurant_by_id` → `restaurant["location_id"]` directo.
- Si no → query explícita: `SELECT id FROM locations WHERE org_id = $1 ORDER BY id ASC LIMIT 1`.

**Forward guard test:** test AST que falle CI si alguien re-introduce `or org_id` después de `restaurant.get("location_id")`. Patrón ya existe (`test_no_parent_restaurant_id_sql.py`, `test_no_is_primary_sql.py`).

**Comportamiento de `"all"` SE QUEDA INTACTO** — es feature, no bug.

**Acciones pendientes:** ninguna.

---

## Duda 8 — `adjust_table_bill` sin upper bound

**Pregunta original:** *"¿Es deliberado para que un cajero pueda 'perdonar' la cuenta entera? Si sí, debería loguear AUDITORÍA explícita. Si no, es vector de fraude."*

**Contexto en código:**
- [tables.py:976-995](../app/routes/tables.py): valida solo `new_total >= 0`. No requiere razón. No loguea quién lo hizo. Vector de fraude operativo.

**Respuesta del PM:** *"Sí, claramente es un fallo confiar en la buena fe. Se debe tener audit de eso."*

**Implicación para el plan:**

Sesión nueva propia: **"Audit de ajustes de cuenta"**. Esfuerzo medio (~3h).

Cambios:
1. **Mantener sin upper bound** (perdonar cuenta entera sigue siendo legal).
2. **Body request requiere `reason: str`** (longitud ≥5 chars). UI predefinida: "Descuento promocional / Cortesía gerente / Plato defectuoso / Cliente VIP / Otro".
3. **Logging estructurado completo** en cada ajuste: `user_id, user_name, user_role, original_total, new_total, delta, delta_pct, reason, branch_id`.
4. **Tabla nueva `bill_adjustments`** (migración Alembic): `id, org_id, location_id, base_order_id, user_id, original_total, new_total, reason, created_at`. RLS por `org_id`.
5. **Visual admin "Ajustes de cuenta"**: filtros por fecha, sucursal, cajero, % descuento. Export CSV.
6. **Backlog Tier 2 (NO esta sesión):** si `delta_pct > 50%` y rol no es `owner|admin`, exigir confirmación de PIN gerente.

**Acciones pendientes:** ninguna.

---

## Duda 9 — PIN login con `restaurant_id` cliente-supplied

**Pregunta original:** *"¿Es porque el QR/login flow no tiene aún el restaurant_id en sesión? Si es legítimo, agregar cross-check post-lookup que el staff retornado pertenezca al restaurant_id supplied."*

**Contexto en código:**
- [staff.py:187-194](../app/routes/staff.py): endpoint sin auth previo (es el AUTH).
- [staff_repo.py:220-232](../app/repositories/staff_repo.py): SQL ya tiene `WHERE org_id=$1`.

**Respuesta del PM:** *"Sí, es redundante. Sí, todo sea por la seguridad del producto y servicio."*

**Implicación para el plan:**

Corrección al audit: la sugerencia "cross-check ownership" es redundante porque la SQL ya filtra por `org_id`. Pero hay un vector real diferente: rate-limit per `IP+restaurant_id+name` permite iteración multi-tenant.

Sesión 9 hace:
1. **Mantener `restaurant_id` en body** — es legítimo.
2. Agregar **rate-limit GLOBAL por IP** independiente de restaurant+name:
   ```python
   await state_store.rate_limit_check(
       key=f"pin_login_global:{client_ip}",
       max_requests=30,
       window_seconds=900,  # 15min
   )
   ```
3. **Audit log de cada intento fallido**: `log.warning("staff.pin_login_failed", ip=..., restaurant_id=..., name=...)`.
4. **Backlog (NO esta sesión):** forzar PIN mínimo de 6 dígitos en creación nueva.

**Acciones pendientes:** ninguna.

---

## Duda 10 — Auth fallback SHA-256 plano

**Pregunta original:** *"¿Cuántos hashes legacy quedan en producción? Si son menos de N, force-rehash en login y dropear el fallback."*

**Contexto en código:**
- [auth.py:53-68](../app/services/auth.py): fallback SHA-256 + opportunistic upgrade en login (líneas 80-91).
- **El force-rehash en login YA ESTÁ implementado** como opportunistic upgrade — el sistema se auto-decommissiona solo.

**Respuesta del PM:** *"Todos los usuarios actuales son pruebas que yo mismo he creado, no hay riesgo en borrarlos."*

**Implicación para el plan:**

Sesión 10 simplificada (PM no tiene clientes reales todavía):
1. Eliminar bloque try/except con fallback SHA-256 en `verify_password`.
2. Eliminar función `_is_legacy_hash`.
3. Eliminar opportunistic upgrade en `login()`.
4. Eliminar call-sites de `db_update_user_password_hash` / `db_update_staff_pin_hash` (solo existen para upgrade).
5. Script `scripts/wipe_test_users.py` que borra users con hash no-bcrypt (PM lo corre a mano una vez).
6. **NO hacer migración Alembic con DELETE** — riesgo de correr accidentalmente con users reales en el futuro. Script manual es más seguro.

`auth.py` queda en ~250 LOC (de ~303).

**Acciones pendientes:** PM corre `wipe_test_users.py` antes de cerrar la sesión.

---

# Ajustes al REMEDIATION_PLAN

Las 10 dudas resueltas implican estos ajustes al plan original:

## Sesiones que cambian de scope

| Sesión original | Cambio |
|---|---|
| Sesión 2 (endpoints sin auth) | Confirmado: borrar `/cart/clear` (zero callers reales). |
| Sesión 7 (Matriz invariant) | Confirmado: ataca solo los 5 sitios + agrega test guard AST. NO toca `X-Branch-ID="all"`. |
| Sesión 10 (auth) | Simplificada: borrado total del fallback + script `wipe_test_users.py`. PM no tiene users reales. |

## Sesiones nuevas a agregar al plan (no estaban en las 10 originales)

Cada una destapada por la entrevista PM. Prioridad relativa entre paréntesis.

1. **Owner bootstrap workflow en superadmin** (Alta) — destapado por duda 4. Hoy crear restaurante NO crea owner user ni link de login. Sin esto no hay onboarding real.
2. **Slug migration** (Media-Alta) — duda 4. Reemplaza `?r=N` por `?r=slug`. Migración + 6 cambios de URL.
3. **Botón "Vaciar carrito" en menú web** (Baja) — duda 5. Frontend localStorage puro. Trivial.
4. **Tool `clear_cart` en bot WhatsApp** (Media) — duda 5. Tool def + confirmación + dedup.
5. **Quick-invoice menú-first + items personalizados auditables** (Media) — duda 6. Mini-feature con migración + UI.
6. **Audit de ajustes de cuenta (`bill_adjustments`)** (Alta) — duda 8. Tabla audit + reason + visual admin.
7. **Audit + completar feature flags modulares** (Alta) — destapado por entrevista meta-4. Crear flags faltantes (`module_loyalty`, `module_payroll`, `module_webauthn`, `module_marketing`) y gate UI completo.
8. **Pricing infrastructure: token tracking + module limits + enforcement** (Alta) — destapado por entrevista meta-5. Sin esto, fair-use es ficción y Mesio puede perder plata por cliente chatty.
9. **Demo Infrastructure** (Media-Alta, post-bugs) — destapado por entrevista meta-11. Demo restaurant seed + dashboard sembrado + `/demo-chat.html` rebuild + landing rebuild.

## Pre-condiciones nuevas para reactivar features desactivadas

Antes de cambiar `module_loyalty=true` para cualquier cliente: fix bugs #7, #8, #12 del audit (race accrue, Wompi+manual double accrue, XSS phone).

Antes de cambiar `module_marketing=true`: revisar `marketing_messages_log` por race conditions (no auditado en detalle).

## Acciones pendientes que requieren acceso a producción

| Acción | Bloquea sesión |
|---|---|
| Query orphan-orgs en prod (verificar que toda org tiene ≥1 location) | Sesión "dead code" (legacy paths de agent_external) |
| Correr `scripts/wipe_test_users.py` después de eliminar fallback SHA-256 | Sesión 10 (auth cleanup) |
